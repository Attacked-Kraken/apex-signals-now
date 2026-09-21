#!/usr/bin/env python3
"""Apex Signals Now — Instance #2 (cruzbot_instance_2) paper-first engine."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trading_bot.adaptive_scalp import SmartMemory
from trading_bot.agent_core import AgentCore
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.config import Settings, get_settings
from trading_bot.data_feed import DataFeed
from trading_bot.executor import Executor
from trading_bot.grok_client import GrokClient
from trading_bot.market_regime import BtcRegimeEngine
from trading_bot.notifier import Notifier
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import OpsState
from trading_bot.strategy import check_daily_drawdown_circuit
from trading_bot.strategy_volume_sweet_spot import VolumeSweetSpotStrategy
from trading_bot.telegram_commands import (
    CIRCUITY_BREAKER_USAGE,
    REPLY_RESET_PAPER_CONFIRM_EXPIRED,
    REPLY_WIPE_PAPER_CONFIRM_EXPIRED,
    RESET_PAPER_CONFIRM_TTL_SECONDS,
    STATUS_SYMBOLS_CALLBACK,
    STOP_LOSS_PRESETS,
    UNIVERSE_STOCKS_USAGE,
    UNIVERSE_USAGE,
    WEEKLY_DIGEST_USAGE,
    WIPE_PAPER_CONFIRM_TTL_SECONDS,
    ResetPaperError,
    TelegramCommandListener,
    TelegramReply,
    WipePaperError,
    assert_paper_mode_for_reset_paper,
    assert_paper_mode_for_wipe_paper,
    build_weekly_expectancy_digest,
    default_reset_paper_cash,
    execute_set_circuity_breaker,
    execute_set_stop_loss,
    execute_set_trade_profile,
    execute_set_winning_formula,
    format_balance_reply,
    format_circuity_breaker_status,
    format_history_reply,
    format_ping_reply,
    format_positions_reply,
    format_reset_paper_done_reply,
    format_reset_paper_pending_reply,
    format_status_reply,
    status_with_symbols_button,
    format_symbols_reply,
    format_universe_status,
    format_universe_switched,
    format_wipe_paper_done_reply,
    format_wipe_paper_pending_reply,
    normalize_symbol_mode,
    parse_circuity_breaker_args,
    parse_reset_paper_args,
    parse_universe_args,
    parse_universe_stocks_args,
    parse_weekly_digest_args,
    parse_wipe_paper_args,
    execute_set_spread,
    SetSpreadError,
    parse_set_max_spread_alias,
    read_tail_log_lines,
    recent_trades_from_ledger,
    wipe_paper_artifacts,
)
from trading_bot.utils.decision_filters import maybe_fee_lock_sl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("apex.main")

_WF_BEAR_SL_MAX = 0.0125
_WF_BEAR_MAX_RISK_USD = 6.0
_WF_BEAR_REF_NOTIONAL = 500.0
_HWM_PEAK_ARM_PCT = 0.012
_HWM_FLOOR_PCT = 0.0125
_HWM_PROGRESS_ARM = 60.0
_TRAIL_RUNNER_ARM_PCT = 0.025
_TRAIL_RUNNER_PROGRESS = 75.0
_TRAIL_RUNNER_OFFSET_PCT = 0.01
_TIME_EXIT_FEE_CUSHION_PCT = 0.0125
_TIME_EXIT_MAKER_WAIT_SEC = 300.0
MAJORS_ONLY_DEFAULT = ("BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD")
ENV_PATH = ROOT / ".env"


class TradingApp:
    def __init__(self, settings: Settings, *, force_live_cli: bool = False, dry_run: bool = False):
        self.settings = settings
        self.force_live_cli = force_live_cli
        self.dry_run = dry_run or settings.dry_run
        self.ops = OpsState(
            paused=settings.paused,
            cb_enabled=bool(getattr(settings, "circuit_breaker_enabled", True)),
        )
        self.paper = bool(settings.paper_trading_mode) and not (
            force_live_cli and not settings.paper_trading_mode
        )
        # Live gating: paper by default; live needs PAPER=false AND --live AND /confirm_live
        if force_live_cli and settings.paper_trading_mode:
            logger.warning("--live ignored while PAPER_TRADING_MODE=true")
        self.live_armed = (
            (not settings.paper_trading_mode) and force_live_cli and self.ops.live_confirmed
        )
        self.paper = not self.live_armed

        self.broker = KrakenBroker(
            api_key=settings.kraken_api_key,
            api_secret=settings.kraken_api_secret,
            base_url=settings.kraken_base_url,
            paper=self.paper,
            paper_book_path=settings.paper_book_path,
            account_equity=settings.account_equity,
        )
        self.data_feed = DataFeed(base_url=settings.kraken_base_url)
        self.strategy = VolumeSweetSpotStrategy(settings)
        self.agent = AgentCore(settings, self.data_feed, self.strategy)
        self.risk = RiskManager(settings)
        def _cb_pause(value: bool) -> None:
            self.ops.set_pause(value)
            if value:
                self.ops.arm_cb_auto_resume()

        self.risk.set_ops(_cb_pause, lambda m: asyncio.create_task(self.notifier.send(m)))
        self.executor = Executor(self.broker, dry_run=self.dry_run, post_only=settings.post_only)
        self.regime = BtcRegimeEngine(enabled=settings.btc_regime_enabled)
        self.smart_memory = SmartMemory(settings.trade_memory_path)
        self.grok = GrokClient(
            settings.xai_api_key,
            base_url=settings.xai_base_url,
            model=settings.xai_model,
            min_interval_seconds=settings.xai_min_interval_seconds,
        )
        self.tg: Optional[TelegramCommandListener] = None
        self.notifier = Notifier()
        self._pending_maker_time_exits: Dict[str, Dict[str, Any]] = {}
        self._enforce_winning_formula_sl()

    def _enforce_winning_formula_sl(self) -> None:
        if self.settings.winning_formula:
            execute_set_stop_loss(self.settings, "medium", env_path=ENV_PATH if ENV_PATH.exists() else None)

    # --- regime / thresholds / brackets ---

    def _short_bias(self) -> bool:
        return bool(self.regime.state.short_bias)

    def _effective_max_concurrent(self) -> int:
        if self._short_bias():
            return 1
        return int(self.settings.max_concurrent_positions)

    def _bear_spot_long_threshold(self, base: float) -> float:
        spot_long_only = not self.settings.allow_paper_shorts
        short_bias = self._short_bias()
        if spot_long_only and short_bias:
            floor = 65.0 if self.settings.winning_formula else 50.0
            return max(base * 1.10, floor)
        if self.settings.winning_formula:
            return max(base, 60.0)  # Tier-1 BULL floor
        return base

    def _wants_quick_scalp(self) -> bool:
        return self.settings.trade_profile == "aggressive" or self.settings.entry_threshold <= 35

    def _effective_entry_threshold(self) -> Tuple[float, str]:
        base = float(self.settings.entry_threshold)
        note = ""
        if self._wants_quick_scalp():
            adj = self.smart_memory.effective_threshold(base, wants_quick_scalp=True)
            if adj != base:
                note = self.smart_memory.status_note()
                base = adj
        eff = self._bear_spot_long_threshold(base)
        return eff, note

    def _profile_sl_tp_pct(self) -> Tuple[float, float, bool]:
        """Return (sl_pct, tp_pct, wf_bear_clamped)."""
        key = (self.settings.stop_loss_profile or "medium").lower()
        preset = STOP_LOSS_PRESETS.get(key, STOP_LOSS_PRESETS["medium"])
        sl = float(preset["sl_pct"])
        tp = float(preset["tp_pct"])
        clamped = False
        if self.settings.winning_formula and self._short_bias():
            dollar_cap = _WF_BEAR_MAX_RISK_USD / _WF_BEAR_REF_NOTIONAL
            sl = min(sl, _WF_BEAR_SL_MAX, dollar_cap)
            tp = abs(sl) * 1.5 + 0.008
            clamped = True
        elif self.settings.winning_formula:
            tp = abs(sl) * 1.5 + 0.008
        return sl, tp, clamped

    async def _apply_profile_brackets(
        self, sym: str, entry: float, qty: float = 0.0, short: bool = False
    ) -> None:
        sl_pct, tp_pct, _ = self._profile_sl_tp_pct()
        if short:
            sl = entry * (1.0 + sl_pct)
            tp = entry * (1.0 - tp_pct)
        else:
            sl = entry * (1.0 - sl_pct)
            tp = entry * (1.0 + tp_pct)
        await self.broker.update_position_brackets(sym, sl=sl, tp=tp)

    # --- hard brackets ---

    async def _check_hard_brackets(self) -> None:
        positions = await self.broker.get_positions()
        fee_buf = float(self.settings.trail_fee_buffer_pct or _HWM_FLOOR_PCT)
        arm = float(self.settings.elite_fee_lock_arm_pct or fee_buf)
        for pos in positions:
            ticker = await self.broker.get_ticker(pos.symbol)
            mark = float(ticker.get("mid") or pos.entry)
            upl = pos.unrealized_pnl_pct(mark)
            short = pos.side == "short"
            sl = pos.sl
            tp = pos.tp

            if self.settings.elite_risk_enabled:
                new_sl = maybe_fee_lock_sl(
                    pos.entry,
                    mark,
                    sl,
                    short=short,
                    arm_pct=arm,
                    fee_buffer_pct=fee_buf,
                )
                if new_sl is not None and new_sl != sl:
                    sl = new_sl
                    await self.broker.update_position_brackets(pos.symbol, sl=sl)

            # Peak tracking for HWM / profit-runner
            peak_key = f"peak_upl:{pos.symbol}"
            peak = float(self.ops.extra.get(peak_key) or 0.0)
            peak = max(peak, upl)
            self.ops.extra[peak_key] = peak

            # HWM: peak ≥ +1.20% → SL floor +1.25% (never trail below)
            if (not short) and peak >= _HWM_PEAK_ARM_PCT:
                hwm_floor = pos.entry * (1.0 + _HWM_FLOOR_PCT)
                if sl is None or sl < hwm_floor:
                    sl = hwm_floor
                    await self.broker.update_position_brackets(pos.symbol, sl=sl)

            # Progress vs TP for runner arm
            progress = 0.0
            if tp is not None and pos.entry > 0 and not short:
                tp_dist = (float(tp) - pos.entry) / pos.entry
                if tp_dist > 1e-12:
                    progress = 100.0 * upl / tp_dist

            # Profit-runner trail: arm at ≥+2.50% UPL or 75% progress; 1% behind peak
            runner_armed = (upl >= _TRAIL_RUNNER_ARM_PCT) or (progress >= _TRAIL_RUNNER_PROGRESS)
            if runner_armed and not short:
                notify_key = f"profit_runner_notified:{pos.symbol}"
                if not self.ops.extra.get(notify_key):
                    self.ops.extra[notify_key] = True
                    await self.notifier.send(
                        "🎯 [PROFIT RUNNER] Target >= +2.50% reached. Trailing Stop Activated."
                    )
                    logger.info("PROFIT_RUNNER_ARMED %s upl=%.4f progress=%.1f", pos.symbol, upl, progress)
                trail_sl = mark * (1.0 - _TRAIL_RUNNER_OFFSET_PCT)
                floor = pos.entry * (1.0 + _HWM_FLOOR_PCT) if peak >= _HWM_PEAK_ARM_PCT else pos.entry * (1.0 + fee_buf)
                trail_sl = max(trail_sl, floor)
                if sl is None or trail_sl > sl:
                    sl = trail_sl
                    await self.broker.update_position_brackets(pos.symbol, sl=sl)
            elif upl >= fee_buf and sl is not None:
                # Early fee-buffer trail (pre-runner)
                trail_dist = max(0.004 * pos.entry, abs(mark - pos.entry) * 0.25)
                if short:
                    candidate = mark + trail_dist
                    if candidate < sl:
                        sl = candidate
                        await self.broker.update_position_brackets(pos.symbol, sl=sl)
                else:
                    candidate = mark - trail_dist
                    floor = pos.entry * (1.0 + fee_buf)
                    if peak >= _HWM_PEAK_ARM_PCT:
                        floor = max(floor, pos.entry * (1.0 + _HWM_FLOOR_PCT))
                    candidate = max(candidate, floor)
                    if candidate > (sl or 0):
                        sl = candidate
                        await self.broker.update_position_brackets(pos.symbol, sl=sl)

            # SL / TP hits (full exits only when tp1_fraction==0)
            hit = False
            reason = ""
            if sl is not None:
                if (not short and mark <= sl) or (short and mark >= sl):
                    hit, reason = True, "SL"
            if tp is not None and not hit:
                if (not short and mark >= tp) or (short and mark <= tp):
                    hit, reason = True, "TP"

            # TIME_EXIT maker — only if gross ≥ +1.25%; wait 5 min
            if not hit and pos.opened_at and self.settings.max_hold_minutes > 0:
                from datetime import datetime, timezone

                try:
                    opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                except Exception:  # noqa: BLE001
                    opened = None
                if opened is not None:
                    age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
                    if age_min >= self.settings.max_hold_minutes and upl >= _TIME_EXIT_FEE_CUSHION_PCT:
                        pending = self._pending_maker_time_exits.get(pos.symbol)
                        be_px = pos.entry * (1.0 + _TIME_EXIT_FEE_CUSHION_PCT)
                        now = asyncio.get_event_loop().time()
                        if pending is None:
                            self._pending_maker_time_exits[pos.symbol] = {
                                "be_px": be_px,
                                "deadline": now + _TIME_EXIT_MAKER_WAIT_SEC,
                            }
                            logger.info("TIME_EXIT_MAKER_WAIT %s be=%.4f", pos.symbol, be_px)
                        else:
                            if (not short and mark >= pending["be_px"]) or (short and mark <= pending["be_px"]):
                                hit, reason = True, "TIME_EXIT_MAKER_BE"
                            elif now >= pending["deadline"]:
                                if sl is not None and ((not short and mark <= sl) or (short and mark >= sl)):
                                    hit, reason = True, "TIME_EXIT_MAKER_TIMEOUT_SL"
                                else:
                                    logger.info("TIME_EXIT_MAKER_WAIT %s", pos.symbol)

            if hit:
                self._pending_maker_time_exits.pop(pos.symbol, None)
                self.ops.extra.pop(f"peak_upl:{pos.symbol}", None)
                self.ops.extra.pop(f"profit_runner_notified:{pos.symbol}", None)
                result = await self.executor.sell(pos.symbol, pos.qty, price=mark, reason=reason)
                won = mark > pos.entry if not short else mark < pos.entry
                self.risk.record_trade_result(won, paused=self.ops.paused)
                self.broker.set_consecutive_losses(self.risk.consecutive_losses())
                if self._wants_quick_scalp():
                    self.smart_memory.record(won)
                await self.notifier.send(
                    f"{'✅' if won else '❌'} {reason} {pos.symbol} @ {mark:.4f}"
                )
                if result:
                    logger.info("Exit %s %s → %s", reason, pos.symbol, result.order_id)

    # --- scan / trade ---

    async def _update_btc_regime(self) -> None:
        bars = await self.data_feed.get_ohlc("BTC-USD", interval=15)
        closes = DataFeed.series(bars, "c") if bars else []
        self.regime.update(closes)

    async def run_once(self) -> None:
        self.ops.touch_tick()
        await self._update_btc_regime()
        await self._check_hard_brackets()

        bal = await self.broker.get_balances()
        day_pnl = float(bal["equity"]) - float(bal["day_start_equity"])
        blocked, why = check_daily_drawdown_circuit(day_pnl, float(bal["day_start_equity"]))
        if blocked:
            logger.warning(why)
            return

        thresh, _note = self._effective_entry_threshold()
        # HIGH_VOL halves notional later; exposure check uses settings caps
        symbols = self.settings.symbol_list()
        signals, elapsed_ms = await self.agent.scan(
            symbols, threshold=thresh, short_bias=self._short_bias()
        )
        self.ops.last_scan_ms = elapsed_ms
        self.ops.last_scan_n = len(symbols)

        # focus = best score
        best = max(signals, key=lambda s: s.score) if signals else None
        if best:
            self.ops.focus_symbol = best.symbol
            self.ops.focus_score = best.score
            self.ops.focus_blocked = "" if best.side == "BUY" else (best.reason or "WAIT")

        positions = await self.broker.get_positions()
        open_exposure = float(bal["exposure"])
        max_c = self._effective_max_concurrent()

        for sig in sorted(signals, key=lambda s: s.score, reverse=True):
            if sig.side != "BUY":
                continue
            proposed = float(self.settings.max_notional_per_trade_usd)
            macro = (sig.meta or {}).get("macro")
            if macro == "HIGH_VOLATILITY":
                proposed *= 0.5
            verdict = self.risk.check_entry(
                open_exposure_usd=open_exposure,
                open_positions=len(positions),
                max_concurrent=max_c,
                paused=self.ops.paused,
                score=sig.score,
                threshold=thresh,
                proposed_notional=proposed,
            )
            if not verdict.approved:
                logger.info("Skip %s: %s", sig.symbol, verdict.reason)
                continue
            result = await self.executor.buy(sig.symbol, verdict.sized_notional)
            if result and result.status in ("filled", "dry_run"):
                await self._apply_profile_brackets(sig.symbol, result.price, qty=result.qty)
                await self.notifier.send(
                    f"🟢 BUY {sig.symbol} ${verdict.sized_notional:.0f} @ {result.price:.4f} "
                    f"(score {sig.score:.0f})"
                )
                # refresh after one entry this cycle
                break

    async def run_loop(self) -> None:
        poll = float(self.settings.agent_poll_seconds or 30)
        logger.info(
            "Starting Apex Signals Now (%s) poll=%.0fs strategy=%s",
            "PAPER" if self.paper else "LIVE",
            poll,
            self.settings.strategy_mode,
        )
        while not self.ops.kill_requested:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("loop iteration failed")
            await asyncio.sleep(poll)

    # --- Telegram wiring ---

    def _wire_telegram_commands(self) -> Dict[str, Any]:
        async def status(_cmd: str, _args: List[str]):
            return await self._cmd_status()

        async def pause(_c: str, _a: List[str]) -> str:
            self.ops.set_pause(True)
            return "Paused: new buys skipped; exits/brackets still managed."

        async def resume(_c: str, _a: List[str]) -> str:
            self.ops.set_pause(False)
            return "Resumed: new buys enabled."

        async def pnl(_c: str, _a: List[str]) -> str:
            bal = await self.broker.get_balances()
            return self.notifier.performance_report(bal, paper=self.paper)

        async def kill(_c: str, _a: List[str]) -> str:
            await self.broker.cancel_all()
            positions = await self.broker.get_positions()
            for p in positions:
                await self.executor.sell(p.symbol, p.qty, reason="KILL")
            self.ops.kill_requested = True
            return "Kill: flattened + loop stop requested."

        async def mode(_c: str, args: List[str]) -> str:
            if not args:
                return f"mode={'PAPER' if self.paper else 'LIVE'} (PAPER_TRADING_MODE={self.settings.paper_trading_mode})"
            want = args[0].lower()
            if want == "paper":
                object.__setattr__(self.settings, "paper_trading_mode", True)
                self.paper = True
                self.broker.paper = True
                self.ops.live_confirmed = False
                return "Switched to PAPER (local fills only)."
            if want == "live":
                return (
                    "LIVE requested. Set PAPER_TRADING_MODE=false, restart with --live, "
                    "then /confirm_live."
                )
            return "Usage: /mode [paper|live]"

        async def confirm_live(_c: str, _a: List[str]) -> str:
            if self.settings.paper_trading_mode:
                return "Refuse: PAPER_TRADING_MODE=true. Set false in .env first."
            if not self.force_live_cli:
                return "Refuse: restart with --live then /confirm_live."
            self.ops.live_confirmed = True
            self.live_armed = True
            self.paper = False
            self.broker.paper = False
            return "LIVE confirmed — real orders enabled. Trade carefully."

        async def set_limit(_c: str, args: List[str]) -> str:
            if len(args) < 2:
                return "Usage: /set_limit <trade> <book>"
            trade, book = float(args[0]), float(args[1])
            object.__setattr__(self.settings, "max_notional_per_trade_usd", trade)
            object.__setattr__(self.settings, "max_total_exposure_usd", book)
            object.__setattr__(self.settings, "caps_custom_lock", True)
            if ENV_PATH.exists():
                from trading_bot.telegram_commands import _persist_env
                _persist_env(
                    ENV_PATH,
                    {
                        "MAX_NOTIONAL_PER_TRADE_USD": str(trade),
                        "MAX_TOTAL_EXPOSURE_USD": str(book),
                        "CAPS_CUSTOM_LOCK": "true",
                    },
                )
            return f"caps → ${trade:.0f}/trade ${book:.0f} exposure 🔒"

        async def set_threshold(_c: str, args: List[str]) -> str:
            if not args:
                return f"ENTRY_THRESHOLD={self.settings.entry_threshold}"
            v = float(args[0])
            if not 15 <= v <= 95:
                return "Threshold must be 15–95"
            from trading_bot.telegram_commands import apply_runtime_entry_threshold, _persist_env

            apply_runtime_entry_threshold(self.settings, v)
            if ENV_PATH.exists():
                _persist_env(ENV_PATH, {"ENTRY_THRESHOLD": str(v)})
            return f"Threshold → {v:.0f}%"

        async def set_threshold_custom(_c: str, args: List[str]) -> str:
            reply = await set_threshold(_c, args)
            object.__setattr__(self.settings, "entry_threshold_custom_lock", True)
            if ENV_PATH.exists():
                from trading_bot.telegram_commands import _persist_env
                _persist_env(ENV_PATH, {"ENTRY_THRESHOLD_CUSTOM_LOCK": "true"})
            return reply + " (custom lock)"

        async def set_spread(_c: str, args: List[str]) -> str:
            if not args:
                pct = float(self.settings.max_spread_pct) * 100.0
                return f"max_spread_pct={pct:g}% (fraction {self.settings.max_spread_pct})"
            try:
                return execute_set_spread(
                    self.settings,
                    args,
                    env_path=ENV_PATH if ENV_PATH.exists() else None,
                )
            except SetSpreadError as exc:
                return str(exc)

        async def tod_custom(_c: str, args: List[str]) -> str:
            if not args:
                return f"tod_custom={'ON' if self.settings.tod_gate_enabled else 'OFF'}"
            on = args[0].lower() in ("on", "1", "true")
            object.__setattr__(self.settings, "tod_gate_enabled", on)
            object.__setattr__(self.settings, "disable_tod_gate", not on)
            object.__setattr__(self.settings, "tod_custom_lock", True)
            if ENV_PATH.exists():
                from trading_bot.telegram_commands import _persist_env
                _persist_env(
                    ENV_PATH,
                    {
                        "TOD_GATE_ENABLED": "true" if on else "false",
                        "DISABLE_TOD_GATE": "false" if on else "true",
                        "TOD_CUSTOM_LOCK": "true",
                    },
                )
            return f"tod_custom → {'ON' if on else 'OFF'} 🔒"

        async def stop_loss(_c: str, args: List[str]) -> str:
            if not args:
                sl, tp, clamped = self._profile_sl_tp_pct()
                preset = STOP_LOSS_PRESETS.get(self.settings.stop_loss_profile, STOP_LOSS_PRESETS["medium"])
                extra = " (WF BEAR clamp)" if clamped else ""
                return f"stop_loss: {preset['emoji']} {preset['label']} −{sl*100:.2f}%{extra}"
            msg = execute_set_stop_loss(
                self.settings, args[0], env_path=ENV_PATH if ENV_PATH.exists() else None,
                signal_engine=self.strategy,
            )
            for p in await self.broker.get_positions():
                await self._apply_profile_brackets(p.symbol, p.entry, qty=p.qty, short=p.side == "short")
            return msg

        async def winning_formula(_c: str, args: List[str]) -> str:
            if not args or args[0].lower() == "status":
                if self.settings.winning_formula:
                    # bare /winning_formula while ON re-applies
                    return execute_set_winning_formula(
                        self.settings, enabled=True,
                        env_path=ENV_PATH if ENV_PATH.exists() else None,
                        signal_engine=self.strategy,
                    )
                return "winning_formula: OFF"
            on = args[0].lower() in ("on", "1", "true")
            msg = execute_set_winning_formula(
                self.settings, enabled=on,
                env_path=ENV_PATH if ENV_PATH.exists() else None,
                signal_engine=self.strategy,
            )
            for p in await self.broker.get_positions():
                await self._apply_profile_brackets(p.symbol, p.entry, qty=p.qty, short=p.side == "short")
            return msg

        async def profile_cmd(name: str) -> str:
            return execute_set_trade_profile(
                self.settings, name, env_path=ENV_PATH if ENV_PATH.exists() else None
            )

        async def aggressive(_c: str, _a: List[str]) -> str:
            return await profile_cmd("aggressive")

        async def medium(_c: str, _a: List[str]) -> str:
            return await profile_cmd("medium")

        async def low(_c: str, _a: List[str]) -> str:
            return await profile_cmd("low")

        async def profile(_c: str, args: List[str]) -> str:
            if not args:
                return f"Profile: {self.settings.trade_profile}"
            return await profile_cmd(args[0])

        async def test_trade(_c: str, args: List[str]) -> str:
            if not self.paper:
                return "test_trade is paper-only"
            sym = args[0] if args else "BTC-USD"
            result = await self.executor.buy(sym, 100.0)
            if result:
                await self._apply_profile_brackets(sym, result.price, qty=result.qty)
                return f"Paper test BUY {sym} ~$100 @ {result.price:.4f} id={result.order_id}"
            return "test_trade failed"

        async def reset_paper(_c: str, args: List[str]) -> str:
            try:
                assert_paper_mode_for_reset_paper(paper=self.paper)
                confirm, cash_arg = parse_reset_paper_args(args)
            except ResetPaperError as exc:
                return str(exc)
            if not confirm:
                cash = (
                    float(cash_arg)
                    if cash_arg is not None
                    else default_reset_paper_cash(self.settings.account_equity)
                )
                self.ops.arm_reset_paper_confirm(
                    cash, explicit=cash_arg is not None, ttl=RESET_PAPER_CONFIRM_TTL_SECONDS
                )
                return format_reset_paper_pending_reply(
                    cash, ttl_seconds=RESET_PAPER_CONFIRM_TTL_SECONDS
                )
            if not self.ops.has_pending_reset_paper_confirm():
                return REPLY_RESET_PAPER_CONFIRM_EXPIRED
            pending_cash = self.ops.pending_reset_paper_cash()
            explicit = bool(self.ops.pending_reset_paper_explicit()) or cash_arg is not None
            cash = (
                float(cash_arg)
                if cash_arg is not None
                else float(
                    pending_cash
                    or default_reset_paper_cash(self.settings.account_equity)
                )
            )
            self.ops.clear_reset_paper_confirm()
            self.broker.reset_paper(cash)
            self.risk.set_consecutive_losses(0)
            account_equity_updated = False
            if explicit:
                object.__setattr__(self.settings, "account_equity", cash)
                if ENV_PATH.exists():
                    from trading_bot.telegram_commands import _persist_env
                    _persist_env(ENV_PATH, {"ACCOUNT_EQUITY": str(cash)})
                    account_equity_updated = True
            bal = await self.broker.get_balances()
            return format_reset_paper_done_reply(
                cash,
                equity=float(bal.get("equity") or cash),
                positions=0,
                account_equity_updated=account_equity_updated,
            )

        async def wipe_paper(_c: str, args: List[str]) -> str:
            try:
                assert_paper_mode_for_wipe_paper(paper=self.paper)
                confirm, cash_arg = parse_wipe_paper_args(args)
            except WipePaperError as exc:
                return str(exc)
            if not confirm:
                cash = (
                    float(cash_arg)
                    if cash_arg is not None
                    else default_reset_paper_cash(self.settings.account_equity)
                )
                self.ops.arm_wipe_paper_confirm(
                    cash, explicit=cash_arg is not None, ttl=WIPE_PAPER_CONFIRM_TTL_SECONDS
                )
                return format_wipe_paper_pending_reply(
                    cash, ttl_seconds=WIPE_PAPER_CONFIRM_TTL_SECONDS
                )
            if not self.ops.has_pending_wipe_paper_confirm():
                return REPLY_WIPE_PAPER_CONFIRM_EXPIRED
            pending_cash = self.ops.pending_wipe_paper_cash()
            explicit = bool(self.ops.pending_wipe_paper_explicit()) or cash_arg is not None
            cash = (
                float(cash_arg)
                if cash_arg is not None
                else float(
                    pending_cash
                    or default_reset_paper_cash(self.settings.account_equity)
                )
            )
            self.ops.clear_wipe_paper_confirm()
            self.broker.reset_paper(cash)
            self.risk.set_consecutive_losses(0)
            summary = wipe_paper_artifacts(
                project_root=ROOT,
                sqlite_path=ROOT / self.settings.sqlite_path,
            )
            account_equity_updated = False
            if explicit:
                object.__setattr__(self.settings, "account_equity", cash)
                if ENV_PATH.exists():
                    from trading_bot.telegram_commands import _persist_env
                    _persist_env(ENV_PATH, {"ACCOUNT_EQUITY": str(cash)})
                    account_equity_updated = True
            bal = await self.broker.get_balances()
            return format_wipe_paper_done_reply(
                cash,
                equity=float(bal.get("equity") or cash),
                positions=0,
                account_equity_updated=account_equity_updated,
                history_cleared=True,
                logs_cleared=bool(summary.get("log_truncated")),
                ledger_rows_deleted=int(summary.get("ledger_rows_deleted") or 0),
                trading_tables_cleared=summary.get("trading_tables_cleared") or {},
            )

        async def factory_reset(_c: str, args: List[str]) -> str:
            return await wipe_paper(_c, args)

        async def set_cmd(_c: str, args: List[str]) -> str:
            if args and args[0].lower() in ("aggressive", "medium", "low"):
                return await profile_cmd(args[0])
            # /set MAX_SPREAD_PCT <pct> alias (percent units)
            if args:
                key0 = str(args[0]).strip().upper().replace("-", "_").split("=", 1)[0]
                if key0 in ("MAX_SPREAD_PCT", "SPREAD", "SPREAD_PCT", "MAX_SPREAD"):
                    try:
                        return execute_set_spread(
                            self.settings,
                            args,
                            env_path=ENV_PATH if ENV_PATH.exists() else None,
                            from_alias=True,
                        )
                    except SetSpreadError as exc:
                        return str(exc)
            return "Usage: /set aggressive|medium|low  OR  /set MAX_SPREAD_PCT <pct>"

        async def ping(_c: str, _a: List[str]) -> str:
            import time as _time
            t0 = _time.perf_counter()
            # trivial work so latency reflects handler overhead
            _ = self.paper
            ms = (_time.perf_counter() - t0) * 1000.0
            return format_ping_reply(ms)

        async def positions(_c: str, _a: List[str]) -> str:
            pos = await self.broker.get_positions()
            rows = []
            for p in pos:
                t = await self.broker.get_ticker(p.symbol)
                mark = float(t.get("mid") or p.entry)
                pnl_frac = p.unrealized_pnl_pct(mark)
                sl_pct, tp_pct, _ = self._profile_sl_tp_pct()
                sl = p.entry * (1.0 - sl_pct) if p.side != "short" else p.entry * (1.0 + sl_pct)
                tp = p.entry * (1.0 + tp_pct) if p.side != "short" else p.entry * (1.0 - tp_pct)
                rows.append(
                    {
                        "symbol": p.symbol,
                        "qty": p.qty,
                        "avg_entry_price": p.entry,
                        "unrealized_pl": (mark - p.entry) * p.qty,
                        "pnl_pct": pnl_frac * 100.0,
                        "stop_loss": sl,
                        "take_profit": tp,
                    }
                )
            return format_positions_reply(rows, paper=self.paper)

        async def balance(_c: str, _a: List[str]) -> str:
            bal = await self.broker.get_balances()
            return format_balance_reply(
                paper=self.paper,
                cash=float(bal["cash"]),
                equity=float(bal["equity"]),
                available=float(bal.get("cash") or 0),
                available_label="cash" if self.paper else "available",
            )

        async def history(_c: str, _a: List[str]) -> str:
            ledger = ROOT / "data" / "paper_ledger.db"
            trades = recent_trades_from_ledger(str(ledger), limit=5)
            if not trades:
                bal = await self.broker.get_balances()
                closed = list(bal.get("closed_trades") or [])[-5:]
                mapped = []
                for t in closed:
                    mapped.append(
                        {
                            "when": t.get("when") or t.get("closed_at") or "?",
                            "side": t.get("side") or ("SELL" if t.get("pnl") is not None else "?"),
                            "symbol": t.get("symbol") or "?",
                            "qty": float(t.get("qty") or 0),
                            "price": float(t.get("price") or t.get("exit") or 0),
                            "pnl": float(t.get("pnl") or 0),
                        }
                    )
                trades = mapped
            return format_history_reply(trades)

        async def grok(_c: str, args: List[str]) -> str:
            prompt = " ".join(args) if args else "Brief BTC/crypto sentiment for spot swing entries."
            return await self.grok.analyze_sentiment(prompt)

        async def regime(_c: str, _a: List[str]) -> str:
            await self._update_btc_regime()
            s = self.regime.state
            return f"BTC regime: {s.label} short_bias={s.short_bias} close={s.btc_close:.2f} ema50={s.ema50:.2f}"

        async def logs(_c: str, _a: List[str]) -> str:
            return read_tail_log_lines(project_root=ROOT, n=20)

        async def universe(_c: str, args: List[str]) -> str:
            try:
                mode = parse_universe_args(args)
            except ValueError as exc:
                return str(exc)
            syms = self.settings.symbol_list()
            if mode is None:
                return format_universe_status(
                    mode=self.settings.symbol_mode, count=len(syms)
                )
            object.__setattr__(self.settings, "symbol_mode", mode)
            if ENV_PATH.exists():
                from trading_bot.telegram_commands import _persist_env
                _persist_env(ENV_PATH, {"SYMBOL_MODE": mode})
            refreshing = normalize_symbol_mode(mode) == "DYNAMIC_ALL"
            # Discovery refresh is paper-safe stub: keep allowlist symbols for now.
            count = len(self.settings.symbol_list())
            return format_universe_switched(mode=mode, count=count, refreshing=refreshing)

        async def universe_all(_c: str, _a: List[str]) -> str:
            return await universe(_c, ["all"])

        async def universe_stocks(_c: str, args: List[str]) -> str:
            try:
                desired = parse_universe_stocks_args(args)
            except ValueError as exc:
                return str(exc)
            if not args:
                on = bool(self.settings.universe_stocks)
                return f"universe_stocks: {'ON' if on else 'OFF'} — {UNIVERSE_STOCKS_USAGE}"
            if desired is None:
                # toggle
                new_val = not bool(self.settings.universe_stocks)
            else:
                new_val = bool(desired)
            object.__setattr__(self.settings, "universe_stocks", new_val)
            if ENV_PATH.exists():
                from trading_bot.telegram_commands import _persist_env
                _persist_env(ENV_PATH, {"UNIVERSE_STOCKS": "true" if new_val else "false"})
            return f"universe_stocks → {'ON' if new_val else 'OFF'}"

        async def symbols(_c: str, _a: List[str]) -> str:
            return format_symbols_reply(
                self.settings.symbol_list(), mode=self.settings.symbol_mode
            )

        async def close_cmd(_c: str, args: List[str]) -> str:
            if not args:
                return "Usage: /close <SYM>"
            sym = args[0].upper()
            if not sym.endswith("-USD") and "-" not in sym:
                sym = f"{sym}-USD"
            positions = {p.symbol: p for p in await self.broker.get_positions()}
            if sym not in positions:
                return f"No position {sym}"
            p = positions[sym]
            if len(args) < 2 or args[1].lower() != "confirm":
                fee = p.notional * float(self.settings.maker_fee_rate)
                return f"Close {sym} qty={p.qty:.6f}? Est fee ~${fee:.2f}. Reply /close {sym} confirm"
            await self.executor.sell(sym, p.qty, reason="MANUAL_CLOSE")
            return f"Closed {sym}"

        async def clear_positions(_c: str, _a: List[str]) -> str:
            if not self.paper:
                return "paper only"
            for p in await self.broker.get_positions():
                await self.executor.sell(p.symbol, p.qty, price=p.entry, reason="CLEAR_BE")
            return "Cleared paper positions at BE marks where possible."

        async def weekly_digest_101(_c: str, args: List[str]) -> str:
            try:
                mode = parse_weekly_digest_args(args)
            except ValueError as exc:
                return str(exc)
            if mode is None:
                mode = "paper" if self.paper else "live"
            # LIVE without broker fill pull: digest uses trades_live.db / optional live_closes.
            # Paper path uses local trades.db + trading_bot_2.db (production formatters).
            return build_weekly_expectancy_digest(
                mode=mode,
                root=ROOT,
                trades_db=ROOT / self.settings.trades_db_path,
                memory_db=ROOT / self.settings.sqlite_path,
            )

        async def circuity_breaker_manually(_c: str, args: List[str]) -> str:
            action = parse_circuity_breaker_args(args)
            if action is None:
                return CIRCUITY_BREAKER_USAGE
            enabled = bool(getattr(self.settings, "circuit_breaker_enabled", True))
            consec = int(self.risk.consecutive_losses())
            tripped = bool(self.ops.paused and getattr(self.ops, "cb_active", False))
            if action == "status":
                return format_circuity_breaker_status(
                    enabled=enabled, consec=consec, tripped=tripped
                )
            on = action == "on"
            # Mark trip state when pausing via CB path so status can show TRIPPED.
            if on and self.ops.paused:
                self.ops.cb_active = True
            msg = execute_set_circuity_breaker(
                self.settings,
                enabled=on,
                env_path=ENV_PATH if ENV_PATH.exists() else None,
                ops=self.ops,
                consec=consec,
                tripped=tripped if on else False,
            )
            return msg

        async def help_cmd(_c: str, _a: List[str]) -> str:
            from trading_bot.telegram_commands import BOT_COMMAND_SPECS

            return "\n".join(f"/{c} — {d}" for c, d in BOT_COMMAND_SPECS)

        return {
            "status": status,
            "pause": pause,
            "resume": resume,
            "pnl": pnl,
            "kill": kill,
            "mode": mode,
            "confirm_live": confirm_live,
            "set_limit": set_limit,
            "set_threshold": set_threshold,
            "set_threshold_custom": set_threshold_custom,
            "set_spread": set_spread,
            "tod_custom": tod_custom,
            "stop_loss": stop_loss,
            "winning_formula": winning_formula,
            "aggressive": aggressive,
            "medium": medium,
            "low": low,
            "profile": profile,
            "test_trade": test_trade,
            "reset_paper": reset_paper,
            "wipe_paper": wipe_paper,
            "factory_reset": factory_reset,
            "set": set_cmd,
            "ping": ping,
            "positions": positions,
            "balance": balance,
            "history": history,
            "grok": grok,
            "regime": regime,
            "logs": logs,
            "universe": universe,
            "universe_all": universe_all,
            "universe_stocks": universe_stocks,
            "symbols": symbols,
            "close": close_cmd,
            "clear_positions": clear_positions,
            "weekly_digest_101": weekly_digest_101,
            "circuity_breaker_manually": circuity_breaker_manually,
            "help": help_cmd,
        }

    async def _cmd_status(self):
        bal = await self.broker.get_balances()
        positions = await self.broker.get_positions()
        thresh, note = self._effective_entry_threshold()
        sl_pct, _tp, clamped = self._profile_sl_tp_pct()

        pos_rows = []
        for p in positions:
            tkr = await self.broker.get_ticker(p.symbol)
            mark = float(tkr.get("mid") or p.entry)
            upl = (mark - p.entry) * p.qty if p.side != "short" else (p.entry - mark) * p.qty
            pos_rows.append(
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_entry_price": p.entry,
                    "mark_price": mark,
                    "market_value": abs(p.qty * mark),
                    "unrealized_pl": upl,
                    "side": p.side,
                    "stop_loss": p.sl,
                    "take_profit": p.tp,
                }
            )

        focus_sym = self.ops.focus_symbol or None
        focus_px = None
        if focus_sym:
            tkr = await self.broker.get_ticker(focus_sym)
            focus_px = float(tkr.get("mid") or 0) or None

        market = self.regime.state.label or "n/a"
        if self._short_bias() and "BIAS=" not in market.upper():
            market = f"{market} bias=SHORT"

        wins = int(bal.get("wins") or 0)
        losses = int(bal.get("losses") or 0)
        wr = (100.0 * wins / (wins + losses)) if (wins + losses) else None

        try:
            wallet_b4 = float(self.broker.paper_wallet_b4()) if self.paper else float(bal.get("equity") or 0)
        except Exception:
            wallet_b4 = float(getattr(self.settings, "account_equity", 0) or 0)

        symbols = self.settings.symbol_list()
        if getattr(self.settings, "majors_only", False):
            majors = [s for s in MAJORS_ONLY_DEFAULT if s in symbols] or list(MAJORS_ONLY_DEFAULT)
            status_symbols = majors
        else:
            majors = list(MAJORS_ONLY_DEFAULT)
            status_symbols = symbols

        def _is_xstock(sym: str) -> bool:
            base = str(sym).split("-", 1)[0]
            return base.endswith("x") or base.endswith("X")

        stock_count = sum(1 for s in symbols if _is_xstock(s))

        tod_enabled = bool(self.settings.tod_gate_enabled) and not bool(self.settings.disable_tod_gate)

        entry_proximity = {
            "score": float(self.ops.focus_score or 0.0),
            "symbol": focus_sym,
            "price": focus_px,
            "direction": "WAIT" if self._short_bias() else ("LONG" if float(self.ops.focus_score or 0) >= thresh else "WAIT"),
        }

        # CB banner: ON when enabled (counts always); trip state is consec losses / pause.
        cb_enabled = bool(getattr(self.settings, "circuit_breaker_enabled", True))
        if hasattr(self.ops, "cb_enabled"):
            cb_enabled = bool(getattr(self.ops, "cb_enabled", cb_enabled))

        text = format_status_reply(
            paper_cash=float(bal["cash"]),
            paper_equity=float(bal["equity"]),
            wallet_b4=wallet_b4,
            positions=pos_rows,
            paused=self.ops.paused,
            strategy_mode=str(self.settings.strategy_mode or "volume_sweet_spot"),
            last_tick_age_seconds=self.ops.tick_age_seconds(),
            paper=self.paper,
            symbols=status_symbols,
            max_notional_per_trade=float(self.settings.max_notional_per_trade_usd),
            max_total_exposure=float(self.settings.max_total_exposure_usd),
            target_setup=entry_proximity["direction"],
            entry_proximity=entry_proximity,
            focus_symbol=focus_sym,
            focus_price=focus_px,
            entry_threshold=thresh,
            max_spread_pct=float(self.settings.max_spread_pct),
            trade_profile=self.settings.trade_profile,
            last_scan_latency_ms=self.ops.last_scan_ms,
            last_scan_pair_count=self.ops.last_scan_n,
            market_state=market,
            win_rate_pct=wr,
            session_wins=wins,
            session_losses=losses,
            symbol_mode=self.settings.symbol_mode,
            universe_stocks=bool(self.settings.universe_stocks),
            stock_count=stock_count,
            spot_long_only=not bool(self.settings.allow_paper_shorts),
            focus_block_reason=self.ops.focus_blocked or None,
            tod_gate_enabled=tod_enabled,
            tod_custom_lock=bool(self.settings.tod_custom_lock),
            stop_loss_profile=self.settings.stop_loss_profile,
            stop_loss_effective_pct=sl_pct,
            stop_loss_clamped=clamped,
            winning_formula=bool(self.settings.winning_formula),
            circuit_breaker_on=cb_enabled,
            circuit_breaker_consec_losses=int(self.risk.consecutive_losses()),
            caps_locked=bool(getattr(self.settings, "caps_custom_lock", False)),
            majors_only=bool(getattr(self.settings, "majors_only", True)),
            majors_symbols=majors,
        )
        n = len(list(status_symbols or []))
        if n > 0:
            return status_with_symbols_button(text, symbol_count=n)
        return TelegramReply(text=text)

    def _wire_telegram_callbacks(self) -> Dict[str, Any]:
        async def status_symbols_expand(_data: str):
            return format_symbols_reply(
                self.settings.symbol_list(), mode=self.settings.symbol_mode
            )

        return {STATUS_SYMBOLS_CALLBACK: status_symbols_expand}

    async def start_telegram(self) -> Optional[asyncio.Task]:
        if not self.settings.telegram_commands_enabled:
            return None
        self.tg = TelegramCommandListener(
            self.settings.telegram_bot_token,
            self.settings.telegram_chat_id,
            self._wire_telegram_commands(),
            enabled=True,
            callback_handlers=self._wire_telegram_callbacks(),
        )
        self.notifier.telegram = self.tg
        return asyncio.create_task(self.tg.run(), name="telegram")

    async def shutdown(self) -> None:
        if self.tg:
            await self.tg.stop()
            await self.tg.close()
        await self.broker.close()
        await self.data_feed.close()
        await self.grok.close()


async def amain(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Apex Signals Now Instance #2")
    parser.add_argument("--once", action="store_true", help="Single scan cycle then exit")
    parser.add_argument("--dry-run", action="store_true", help="Log orders without filling")
    parser.add_argument("--live", action="store_true", help="Allow LIVE (still needs PAPER=false + /confirm_live)")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    app = TradingApp(settings, force_live_cli=args.live, dry_run=args.dry_run)
    tg_task = await app.start_telegram()
    try:
        if args.once:
            await app.run_once()
            # print status for smoke
            _st = await app._cmd_status()
            print(_st.text if isinstance(_st, TelegramReply) else _st)
        else:
            loop_task = asyncio.create_task(app.run_loop(), name="loop")
            await loop_task
    finally:
        if tg_task:
            tg_task.cancel()
            try:
                await tg_task
            except asyncio.CancelledError:
                pass
        await app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
