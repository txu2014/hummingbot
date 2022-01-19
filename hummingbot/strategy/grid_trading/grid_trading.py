import logging
import time

from decimal import Decimal
from math import (
    floor,
    ceil
)
from typing import (
    Dict,
    List,
)

import numpy as np
import pandas as pd
from itertools import chain

from hummingbot.connector.derivative.perpetual_budget_checker import PerpetualOrderCandidate
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.exchange_base import ExchangeBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.event.events import (
    BuyOrderCompletedEvent,
    OrderFilledEvent,
    OrderType,
    PositionAction,
    PositionMode,
    PriceType,
    SellOrderCompletedEvent,
    TradeType,
)
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils import map_df_to_str
from hummingbot.strategy.asset_price_delegate import AssetPriceDelegate
from hummingbot.strategy.market_trading_pair_tuple import MarketTradingPairTuple
from hummingbot.strategy.order_book_asset_price_delegate import OrderBookAssetPriceDelegate
from hummingbot.strategy.grid_trading.data_types import (
    PriceSize,
    Proposal,
)
from hummingbot.strategy.strategy_py_base import StrategyPyBase
from .grid_trading_order_tracker import GridTradingMakingOrderTracker

NaN = float("nan")
s_decimal_zero = Decimal(0)
s_decimal_neg_one = Decimal(-1)


class GridTradingStrategy(StrategyPyBase):
    OPTION_LOG_CREATE_ORDER = 1 << 3
    OPTION_LOG_MAKER_ORDER_FILLED = 1 << 4
    OPTION_LOG_STATUS_REPORT = 1 << 5
    OPTION_LOG_ALL = 0x7fffffffffffffff
    _logger = None

    @classmethod
    def logger(cls):
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def init_params(self,
                    market_info: MarketTradingPairTuple,
                    leverage: int,
                    position_mode: str,
                    # grid trading
                    trading_direction: str,
                    lower_bound: Decimal,
                    upper_bound: Decimal,
                    max_amount: Decimal,
                    price_interval: Decimal,

                    bid_spread: Decimal,
                    ask_spread: Decimal,
                    order_amount: Decimal,
                    long_profit_taking_spread: Decimal,
                    short_profit_taking_spread: Decimal,
                    stop_loss_spread: Decimal,
                    time_between_stop_loss_orders: float,
                    stop_loss_slippage_buffer: Decimal,
                    order_levels: int = 1,
                    order_level_spread: Decimal = s_decimal_zero,
                    order_level_amount: Decimal = s_decimal_zero,
                    order_refresh_time: float = 30.0,
                    order_refresh_tolerance_pct: Decimal = s_decimal_neg_one,
                    filled_order_delay: float = 60.0,
                    order_optimization_enabled: bool = False,
                    ask_order_optimization_depth: Decimal = s_decimal_zero,
                    bid_order_optimization_depth: Decimal = s_decimal_zero,
                    asset_price_delegate: AssetPriceDelegate = None,
                    price_type: str = "mid_price",
                    price_ceiling: Decimal = s_decimal_neg_one,
                    price_floor: Decimal = s_decimal_neg_one,
                    logging_options: int = OPTION_LOG_ALL,
                    status_report_interval: float = 900,
                    minimum_spread: Decimal = Decimal(0),
                    hb_app_notification: bool = False,
                    # order_override: Dict[str, List[str]] = {},

                    # grid trading
                    size_of_order: Decimal = None,
                    trigger_price: Decimal = None,
                    ):

        if price_ceiling != s_decimal_neg_one and price_ceiling < price_floor:
            raise ValueError("Parameter price_ceiling cannot be lower than price_floor.")

        self._sb_order_tracker = GridTradingMakingOrderTracker()
        self._market_info = market_info
        self._leverage = leverage
        self._position_mode = PositionMode.HEDGE if position_mode == "Hedge" else PositionMode.ONEWAY

        # grid trading
        self._trading_direction = trading_direction
        self._lower_bound = lower_bound
        self._upper_bound = upper_bound
        self._max_amount = max_amount
        self._price_interval = price_interval
        self._size_of_order = size_of_order
        self._trigger_price = trigger_price

        self._bid_spread = bid_spread
        self._ask_spread = ask_spread
        self._minimum_spread = minimum_spread
        self._order_amount = order_amount
        self._long_profit_taking_spread = long_profit_taking_spread
        self._short_profit_taking_spread = short_profit_taking_spread
        self._stop_loss_spread = stop_loss_spread
        self._order_levels = order_levels
        self._buy_levels = order_levels
        self._sell_levels = order_levels
        self._order_level_spread = order_level_spread
        self._order_level_amount = order_level_amount
        self._order_refresh_time = order_refresh_time
        self._order_refresh_tolerance_pct = order_refresh_tolerance_pct
        self._filled_order_delay = filled_order_delay
        self._order_optimization_enabled = order_optimization_enabled
        self._ask_order_optimization_depth = ask_order_optimization_depth
        self._bid_order_optimization_depth = bid_order_optimization_depth
        self._asset_price_delegate = asset_price_delegate
        self._price_type = self.get_price_type(price_type)
        self._price_ceiling = price_ceiling
        self._price_floor = price_floor
        self._hb_app_notification = hb_app_notification
        # self._order_override = order_override

        self._cancel_timestamp = 0
        self._create_timestamp = 0
        self._all_markets_ready = False
        self._logging_options = logging_options
        self._last_timestamp = 0
        self._status_report_interval = status_report_interval
        self._last_own_trade_price = Decimal('nan')
        self._ts_peak_bid_price = Decimal('0')
        self._ts_peak_ask_price = Decimal('0')
        self._exit_orders = dict()
        self._next_buy_exit_order_timestamp = 0
        self._next_sell_exit_order_timestamp = 0

        self.add_markets([market_info.market])
        self._close_order_type = OrderType.LIMIT
        self._time_between_stop_loss_orders = time_between_stop_loss_orders
        self._stop_loss_slippage_buffer = stop_loss_slippage_buffer

        u = Decimal(self._upper_bound)
        l = Decimal(self._lower_bound)

        if self._price_interval > 0:
            n = int((u - l) / self._price_interval)
        else:
            n = self._number_of_grid
        self.list_price = [Decimal(f"{(l + i * (u - l) / n):.16}") for i in range(n + 1)]
        self.n = n
        self.size = self._max_amount / self.n


    def all_markets_ready(self):
        return all([market.ready for market in self.active_markets])

    @property
    def order_refresh_tolerance_pct(self) -> Decimal:
        return self._order_refresh_tolerance_pct

    @order_refresh_tolerance_pct.setter
    def order_refresh_tolerance_pct(self, value: Decimal):
        self._order_refresh_tolerance_pct = value

    @property
    def order_amount(self) -> Decimal:
        return self._order_amount

    @order_amount.setter
    def order_amount(self, value: Decimal):
        self._order_amount = value

    @property
    def order_levels(self) -> int:
        return self._order_levels

    @order_levels.setter
    def order_levels(self, value: int):
        self._order_levels = value
        self._buy_levels = value
        self._sell_levels = value

    @property
    def buy_levels(self) -> int:
        return self._buy_levels

    @buy_levels.setter
    def buy_levels(self, value: int):
        self._buy_levels = value

    @property
    def sell_levels(self) -> int:
        return self._sell_levels

    @sell_levels.setter
    def sell_levels(self, value: int):
        self._sell_levels = value

    @property
    def order_level_amount(self) -> Decimal:
        return self._order_level_amount

    @order_level_amount.setter
    def order_level_amount(self, value: Decimal):
        self._order_level_amount = value

    @property
    def order_level_spread(self) -> Decimal:
        return self._order_level_spread

    @order_level_spread.setter
    def order_level_spread(self, value: Decimal):
        self._order_level_spread = value

    @property
    def bid_spread(self) -> Decimal:
        return self._bid_spread

    @bid_spread.setter
    def bid_spread(self, value: Decimal):
        self._bid_spread = value

    @property
    def ask_spread(self) -> Decimal:
        return self._ask_spread

    @ask_spread.setter
    def ask_spread(self, value: Decimal):
        self._ask_spread = value

    @property
    def order_optimization_enabled(self) -> bool:
        return self._order_optimization_enabled

    @order_optimization_enabled.setter
    def order_optimization_enabled(self, value: bool):
        self._order_optimization_enabled = value

    @property
    def order_refresh_time(self) -> float:
        return self._order_refresh_time

    @order_refresh_time.setter
    def order_refresh_time(self, value: float):
        self._order_refresh_time = value

    @property
    def filled_order_delay(self) -> float:
        return self._filled_order_delay

    @filled_order_delay.setter
    def filled_order_delay(self, value: float):
        self._filled_order_delay = value

    @property
    def price_ceiling(self) -> Decimal:
        return self._price_ceiling

    @price_ceiling.setter
    def price_ceiling(self, value: Decimal):
        self._price_ceiling = value

    @property
    def price_floor(self) -> Decimal:
        return self._price_floor

    @price_floor.setter
    def price_floor(self, value: Decimal):
        self._price_floor = value

    @property
    def base_asset(self):
        return self._market_info.base_asset

    @property
    def quote_asset(self):
        return self._market_info.quote_asset

    @property
    def trading_pair(self):
        return self._market_info.trading_pair

    def get_price(self) -> float:
        if self._asset_price_delegate is not None:
            price_provider = self._asset_price_delegate
        else:
            price_provider = self._market_info
        if self._price_type is PriceType.LastOwnTrade:
            price = self._last_own_trade_price
        else:
            price = price_provider.get_price_by_type(self._price_type)
        if price.is_nan():
            price = price_provider.get_price_by_type(PriceType.MidPrice)
        return price

    def get_last_price(self) -> float:
        return self._market_info.get_last_price()

    def get_mid_price(self) -> Decimal:
        delegate: AssetPriceDelegate = self._asset_price_delegate
        if delegate is not None:
            mid_price = delegate.get_mid_price()
        else:
            mid_price = self._market_info.get_mid_price()
        return mid_price

    @property
    def active_orders(self) -> List[LimitOrder]:
        if self._market_info not in self._sb_order_tracker.market_pair_to_active_orders:
            return []
        return self._sb_order_tracker.market_pair_to_active_orders[self._market_info]

    @property
    def active_positions(self) -> Dict[str, Position]:
        return self._market_info.market.account_positions

    @property
    def active_buys(self) -> List[LimitOrder]:
        return [o for o in self.active_orders if o.is_buy]

    @property
    def active_sells(self) -> List[LimitOrder]:
        return [o for o in self.active_orders if not o.is_buy]

    @property
    def logging_options(self) -> int:
        return self._logging_options

    @logging_options.setter
    def logging_options(self, logging_options: int):
        self._logging_options = logging_options

    @property
    def asset_price_delegate(self) -> AssetPriceDelegate:
        return self._asset_price_delegate

    @asset_price_delegate.setter
    def asset_price_delegate(self, value):
        self._asset_price_delegate = value

    def perpetual_mm_assets_df(self) -> pd.DataFrame:
        market, trading_pair, base_asset, quote_asset = self._market_info
        quote_balance = float(market.get_balance(quote_asset))
        available_quote_balance = float(market.get_available_balance(quote_asset))
        data = [
            ["", quote_asset],
            ["Total Balance", round(quote_balance, 4)],
            ["Available Balance", round(available_quote_balance, 4)]
        ]
        df = pd.DataFrame(data=data)
        return df

    def active_orders_df(self) -> pd.DataFrame:
        price = self.get_price()
        active_orders = self.active_orders
        no_sells = len([o for o in active_orders if not o.is_buy])
        active_orders.sort(key=lambda x: x.price, reverse=True)
        columns = ["Level", "is_buy", "price", "Spread", "size", "Age", 'order_id']
        data = []
        lvl_buy, lvl_sell = 0, 0
        for idx in range(0, len(active_orders)):
            order = active_orders[idx]
            level = None
            if order.is_buy:
                level = lvl_buy + 1
                lvl_buy += 1
            else:
                level = no_sells - lvl_sell
                lvl_sell += 1
            spread = 0 if price == 0 else abs(order.price - price) / price
            age = "n/a"
            # // indicates order is a paper order so 'n/a'. For real orders, calculate age.
            if "//" not in order.client_order_id:
                age = pd.Timestamp(int(time.time()) - int(order.client_order_id[-16:]) / 1e6,
                                   unit='s').strftime('%H:%M:%S')
            amount_orig = "" if level is None else self._order_amount + ((level - 1) * self._order_level_amount)
            data.append([
                level,
                order.is_buy,
                float(order.price),
                f"{spread:.2%}",
                # amount_orig,
                float(order.quantity),
                age,
                order.client_order_id
            ])

        return pd.DataFrame(data=data, columns=columns)

    def active_positions_df(self) -> pd.DataFrame:
        columns = ["Symbol", "Type", "Entry Price", "Amount", "Leverage", "Unrealized PnL"]
        data = []
        market, trading_pair = self._market_info.market, self._market_info.trading_pair
        for idx in self.active_positions.values():
            is_buy = True if idx.amount > 0 else False
            unrealized_profit = ((market.get_price(trading_pair, is_buy) - idx.entry_price) * idx.amount)
            data.append([
                idx.trading_pair,
                idx.position_side.name,
                idx.entry_price,
                idx.amount,
                idx.leverage,
                unrealized_profit
            ])

        return pd.DataFrame(data=data, columns=columns)

    def market_status_data_frame(self) -> pd.DataFrame:
        markets_data = []
        markets_columns = ["Exchange", "Market", "Best Bid", "Best Ask", f"Ref Price ({self._price_type.name})"]
        if self._price_type is PriceType.LastOwnTrade and self._last_own_trade_price.is_nan():
            markets_columns[-1] = "Ref Price (MidPrice)"
        market_books = [(self._market_info.market, self._market_info.trading_pair)]
        if type(self._asset_price_delegate) is OrderBookAssetPriceDelegate:
            market_books.append((self._asset_price_delegate.market, self._asset_price_delegate.trading_pair))
        for market, trading_pair in market_books:
            bid_price = market.get_price(trading_pair, False)
            ask_price = market.get_price(trading_pair, True)
            ref_price = float("nan")
            if market == self._market_info.market and self._asset_price_delegate is None:
                ref_price = self.get_price()
            elif market == self._asset_price_delegate.market and self._price_type is not PriceType.LastOwnTrade:
                ref_price = self._asset_price_delegate.get_price_by_type(self._price_type)
            markets_data.append([
                market.display_name,
                trading_pair,
                float(bid_price),
                float(ask_price),
                float(ref_price)
            ])
        return pd.DataFrame(data=markets_data, columns=markets_columns).replace(np.nan, '', regex=True)

    def format_status(self) -> str:
        if not self._all_markets_ready:
            return "Market connectors are not ready."
        lines = []
        warning_lines = []

        markets_df = self.market_status_data_frame()
        lines.extend(["", "  Markets:"] + ["    " + line for line in markets_df.to_string(index=False).split("\n")])

        assets_df = map_df_to_str(self.perpetual_mm_assets_df())

        first_col_length = max(*assets_df[0].apply(len))
        df_lines = assets_df.to_string(index=False, header=False,
                                       formatters={0: ("{:<" + str(first_col_length) + "}").format}).split("\n")
        lines.extend(["", "  Assets:"] + ["    " + line for line in df_lines])

        # See if there're any open orders.
        if len(self.active_orders) > 0:
            df = self.active_orders_df()
            lines.extend(["", "  Orders:"] + ["    " + line for line in df.to_string(index=False).split("\n")])
        else:
            lines.extend(["", "  No active maker orders."])

        # See if there're any active positions.
        if len(self.active_positions) > 0:
            df = self.active_positions_df()
            lines.extend(["", "  Positions:"] + ["    " + line for line in df.to_string(index=False).split("\n")])
        else:
            lines.extend(["", "  No active positions."])

        if len(warning_lines) > 0:
            lines.extend(["", "*** WARNINGS ***"] + warning_lines)

        return "\n".join(lines)

    def start(self, clock: Clock, timestamp: float):
        super().start(clock, timestamp)
        self._last_timestamp = timestamp
        self.apply_initial_settings(self.trading_pair, self._position_mode, self._leverage)
        self.logger().info('start() finished')

    def apply_initial_settings(self, trading_pair: str, position: Position, leverage: int):
        self.logger().info(['start startegy', trading_pair, leverage, position])
        market: ExchangeBase = self._market_info.market
        market.set_leverage(trading_pair, leverage)
        market.set_position_mode(position)

    def tick(self, timestamp: float):
        market: ExchangeBase = self._market_info.market
        session_positions = [s for s in self.active_positions.values() if s.trading_pair == self.trading_pair]
        # self.logger().debug(['session positions', session_positions])
        current_tick = timestamp // self._status_report_interval
        last_tick = self._last_timestamp // self._status_report_interval
        should_report_warnings = ((current_tick > last_tick) and
                                  (self._logging_options & self.OPTION_LOG_STATUS_REPORT))
        # self.logger().debug(['new tick', current_tick, last_tick, should_report_warnings])
        try:
            if not self._all_markets_ready:
                self.logger().warning('market not ready _all_markets_ready, waiting ....')
                self._all_markets_ready = all([market.ready for market in self.active_markets])
                if self._asset_price_delegate is not None and self._all_markets_ready:
                    self._all_markets_ready = self._asset_price_delegate.ready
                if not self._all_markets_ready:
                    # M({self.trading_pair}) Maker sell order {order_id}arkets not ready yet. Don't do anything.
                    if should_report_warnings:
                        self.logger().warning("Markets are not ready. No market making trades are permitted.")
                    return

            # self.logger().debug('_all_markets_ready')

            if should_report_warnings:
                if not all([market.network_status is NetworkStatus.CONNECTED for market in self.active_markets]):
                    self.logger().warning("WARNING: Some markets are not connected or are down at the moment. Market "
                                          "making may be dangerous when markets or networks are unstable.")
            # self.logger().debug('should_report_warnings')

            # if len(session_positions) == 0:
            self._exit_orders = dict()  # Empty list of exit order at this point to reduce size
            proposal = None

            if self.current_timestamp >= self._create_timestamp:
                self.set_timers()
                # self.logger().debug("1. Create base order proposals")
                proposal = self.create_base_proposal()
                self.logger().info(['create_base_proposal ', str(proposal)])
                # proposal = self.filter_range(proposal)
                proposal = self.filter_close(proposal)
                proposal = self.reconcile_orders(proposal)
                self.logger().info(['after filter and reconcile ', str(proposal)])
                self.filter_out_takers(proposal)
                proposal = self.filter_limit(proposal)
                self.cancel_orders_below_min_spread()
                # if self.to_create_orders(proposal):
                if proposal is not None:
                    if len(proposal.buys) > 0:
                        self.execute_orders_proposal(Proposal(proposal.buys, []), PositionAction.OPEN)
                    if len(proposal.sells) > 0:
                        self.execute_orders_proposal(Proposal([], proposal.sells), PositionAction.CLOSE)

            # Reset peak ask and bid prices
            self._ts_peak_ask_price = market.get_price(self.trading_pair, False)
            self._ts_peak_bid_price = market.get_price(self.trading_pair, True)

        finally:
            self._last_timestamp = timestamp

    def reconcile_orders(self, proposal: Proposal):
        df_orders = self.active_orders_df()
        self.logger().info(df_orders)
        to_cancel = []
        new_buy = []
        new_sell = []

        if df_orders.empty:
            return proposal

        self.logger().debug(['df_orders', df_orders])
        self.logger().debug(['proposal.buys', proposal.buys])
        self.logger().debug(['proposal.sells', proposal.sells])

        for p in proposal.buys:
            df_match = df_orders[df_orders['price'] == p.price]
            self.logger().debug(['find matching', df_match.shape, df_match])
            order_size = Decimal(f"{df_match['size'].sum():.10}")
            order_ids = df_match['order_id'].to_list()

            if df_match.empty:
                new_buy.append(p)
            elif (abs(p.size - order_size) > 1e-6) or df_match['is_buy'].sum() != df_match.shape[0]:
                self.logger().info(
                    ['replace order', 'buy', p.price, df_match['is_buy'].values, order_size, 'to', p.size, order_ids])
                to_cancel += order_ids
                new_buy.append(p)

        for p in proposal.sells:
            df_match = df_orders[df_orders['price'] == p.price]
            self.logger().debug(['find matching', df_match.shape, df_match])
            order_size = Decimal(f"{df_match['size'].sum():.10}")
            order_ids = df_match['order_id'].to_list()

            if df_match.empty:
                new_sell.append(p)
            elif (abs(p.size - order_size) > 1e-6) or df_match['is_buy'].abs().sum() != 0:
                self.logger().info(
                    ['replace order', 'sell', p.price, df_match['is_buy'].values, order_size, 'to', p.size, order_ids])
                to_cancel += order_ids
                new_sell.append(p)

        proposal.buys = new_buy
        proposal.sells = new_sell
        for i in to_cancel:
            self.cancel_order(self._market_info, i)
        return proposal

    def filter_close(self, proposal: Proposal):
        df_pos = self.active_positions_df()
        total_pos = df_pos['Amount'].sum()
        sells = []
        if self._trading_direction == 'long':  # filter sells
            order_pos = 0
            for o in proposal.sells:
                if order_pos < total_pos:
                    sells.append(o)
                    order_pos += o.size
                else:
                    break
            proposal.sells = sells
        return proposal

    def manage_positions(self, session_positions: List[Position]):
        mode = self._position_mode

        proposals = self.profit_taking_proposal(mode, session_positions)
        if proposals is not None:
            self.execute_orders_proposal(proposals, PositionAction.CLOSE)

        # check if stop loss needs to be placed
        proposals = self.stop_loss_proposal(mode, session_positions)
        if proposals is not None:
            self.execute_orders_proposal(proposals, PositionAction.CLOSE)

    # def profit_taking_proposal(self, mode: PositionMode, active_positions: List) -> Proposal:
    #
    #     market: ExchangeBase = self._market_info.market
    #     unwanted_exit_orders = [o for o in self.active_orders
    #                             if o.client_order_id not in self._exit_orders.keys()]
    #     ask_price = market.get_price(self.trading_pair, True)
    #     bid_price = market.get_price(self.trading_pair, False)
    #     buys = []
    #     sells = []
    #
    #     if mode == PositionMode.ONEWAY:
    #         # in one-way mode, only one active position is expected per time
    #         if len(active_positions) > 1:
    #             self.logger().error(f"More than one open position in {mode.name} position mode. "
    #                                 "Kindly ensure you do not interact with the exchange through "
    #                                 "other platforms and restart this strategy.")
    #         else:
    #             # Cancel open order that could potentially close position before reaching take_profit_limit
    #             for order in unwanted_exit_orders:
    #                 if ((active_positions[0].amount < 0 and order.is_buy)
    #                         or (active_positions[0].amount > 0 and not order.is_buy)):
    #                     self.cancel_order(self._market_info, order.client_order_id)
    #                     self.logger().info(f"Initiated cancellation of {'buy' if order.is_buy else 'sell'} order "
    #                                        f"{order.client_order_id} in favour of take profit order.")
    #
    #     for position in active_positions:
    #         if (ask_price > position.entry_price and position.amount > 0) or (
    #                 bid_price < position.entry_price and position.amount < 0):
    #             # check if there is an active order to take profit, and create if none exists
    #             profit_spread = self._long_profit_taking_spread if position.amount > 0 else self._short_profit_taking_spread
    #             if position.amount > 0:
    #                 take_profit_price = max(self.get_price() * Decimal("1.0001"),
    #                                         position.entry_price * (Decimal("1") + profit_spread))
    #             else:
    #                 take_profit_price = min(self.get_price() * Decimal("0.9999"),
    #                                         position.entry_price * (Decimal("1") - profit_spread))
    #             price = market.quantize_order_price(self.trading_pair, take_profit_price)
    #             size = market.quantize_order_amount(self.trading_pair, abs(position.amount))
    #             old_exit_orders = [
    #                 o for o in self.active_orders
    #                 # if ((o.price != price or o.quantity != size)
    #                 if (o.price != price
    #                     and o.client_order_id in self._exit_orders.keys()
    #                     and ((position.amount < 0 and o.is_buy) or (position.amount > 0 and not o.is_buy)))]
    #             for old_order in old_exit_orders:
    #                 self.cancel_order(self._market_info, old_order.client_order_id)
    #                 self.logger().info(
    #                     f"Initiated cancellation of previous take profit order {old_order.client_order_id} in favour of new take profit order.")
    #             exit_order_exists = [o for o in self.active_orders if o.price == price]
    #             if len(exit_order_exists) == 0:
    #                 if size > 0 and price > 0:
    #                     if position.amount < 0:
    #                         buys.append(PriceSize(price, size))
    #                     else:
    #                         sells.append(PriceSize(price, size))
    #     return Proposal(buys, sells)
    #
    # def _should_renew_stop_loss(self, stop_loss_order: LimitOrder) -> bool:
    #     stop_loss_creation_timestamp = self._exit_orders.get(stop_loss_order.client_order_id)
    #     time_since_stop_loss = self.current_timestamp - stop_loss_creation_timestamp
    #     return time_since_stop_loss >= self._time_between_stop_loss_orders
    #
    # def stop_loss_proposal(self, mode: PositionMode, active_positions: List[Position]) -> Proposal:
    #     market: ExchangeBase = self._market_info.market
    #     top_ask = market.get_price(self.trading_pair, False)
    #     top_bid = market.get_price(self.trading_pair, True)
    #     buys = []
    #     sells = []
    #
    #     for position in active_positions:
    #         # check if stop loss order needs to be placed
    #         stop_loss_price = position.entry_price * (Decimal("1") + self._stop_loss_spread) if position.amount < 0 \
    #             else position.entry_price * (Decimal("1") - self._stop_loss_spread)
    #         existent_stop_loss_orders = [order for order in self.active_orders
    #                                      if order.client_order_id in self._exit_orders.keys()
    #                                      and ((position.amount > 0 and not order.is_buy)
    #                                           or (position.amount < 0 and order.is_buy))]
    #         if (not existent_stop_loss_orders
    #                 or (self._should_renew_stop_loss(existent_stop_loss_orders[0]))):
    #             previous_stop_loss_price = None
    #             for order in existent_stop_loss_orders:
    #                 previous_stop_loss_price = order.price
    #                 self.cancel_order(self._market_info, order.client_order_id)
    #             new_price = previous_stop_loss_price or stop_loss_price
    #             if (top_ask <= stop_loss_price and position.amount > 0):
    #                 price = market.quantize_order_price(
    #                     self.trading_pair,
    #                     new_price * (Decimal(1) - self._stop_loss_slippage_buffer))
    #                 take_profit_orders = [o for o in self.active_orders
    #                                       if (not o.is_buy and o.price > price
    #                                           and o.client_order_id in self._exit_orders.keys())]
    #                 # cancel take profit orders if they exist
    #                 for old_order in take_profit_orders:
    #                     self.cancel_order(self._market_info, old_order.client_order_id)
    #                 size = market.quantize_order_amount(self.trading_pair, abs(position.amount))
    #                 if size > 0 and price > 0:
    #                     self.logger().info("Creating stop loss sell order to close long position.")
    #                     sells.append(PriceSize(price, size))
    #             elif (top_bid >= stop_loss_price and position.amount < 0):
    #                 price = market.quantize_order_price(
    #                     self.trading_pair,
    #                     new_price * (Decimal(1) + self._stop_loss_slippage_buffer))
    #                 take_profit_orders = [o for o in self.active_orders
    #                                       if (o.is_buy and o.price < price
    #                                           and o.client_order_id in self._exit_orders.keys())]
    #                 # cancel take profit orders if they exist
    #                 for old_order in take_profit_orders:
    #                     self.cancel_order(self._market_info, old_order.client_order_id)
    #                 size = market.quantize_order_amount(self.trading_pair, abs(position.amount))
    #                 if size > 0 and price > 0:
    #                     self.logger().info("Creating stop loss buy order to close short position.")
    #                     buys.append(PriceSize(price, size))
    #     return Proposal(buys, sells)

    def gen_order_df(self):
        df_pos = self.active_positions_df()
        current_position = Decimal(f"{(float(df_pos['Amount'].sum())):.16}")

        list_price = self.list_price
        n = self.n
        current_price = self.get_price()

        # initialize
        df_order = pd.DataFrame(list_price, columns=['price']).set_index('price')
        df_order['size'] = Decimal(f"{(self._max_amount / n):.16}")
        df_order['side'] = None
        df_order.loc[:current_price, 'side'] = 'buy'
        df_order.loc[current_price:, 'side'] = 'sell'
        df_order['theo_pos'] = df_order['size'].iloc[-2::-1].cumsum()
        df_order['theo_pos'] = df_order['theo_pos'].fillna(0)

        # initial order
        theo_pos = df_order.loc[current_price:, 'theo_pos'].max()
        pos_diff = theo_pos - current_position
        self.logger().info(f"initial order, price={current_price}, current_pos={current_position}, theo_pos={theo_pos},"
                           f"pos_diff={pos_diff}")

        closest_price = [df_order[:current_price].index.max(),
                         df_order[current_price:].index.min()]

        if pos_diff >= 0:
            side_init = 'buy'
            size_init = Decimal(pos_diff)
            price_init, price_ct = closest_price
        elif pos_diff < 0:
            side_init = 'sell'
            size_init = Decimal(-1) * pos_diff
            price_ct, price_init = closest_price
        size0 = df_order.loc[price_ct, 'size']
        #df_order.loc[price_ct, 'size'] = max(size0*Decimal(0.5), size0 - Decimal(0.1)*size_init)
        df_order.loc[price_init, 'size'] += Decimal(0.02) * size_init
        # self.logger().warning(['gen_order_df', df_order])
        return df_order

    def create_base_proposal(self):
        market: ExchangeBase = self._market_info.market
        buys = []
        sells = []
        n_range = 10
        # df_active = self.active_orders_df()

        df_order = self.gen_order_df()
        df_order_buy = df_order[df_order['side'] == 'buy'].sort_index(ascending=False)
        df_order_sell = df_order[df_order['side'] == 'sell'].sort_index(ascending=True)
        self.logger().debug(['gen_order_df \n', df_order])
        for ix, row in df_order_buy.iloc[:n_range].iterrows():
            price = Decimal(ix)
            size = row['size']
            size = market.quantize_order_amount(self.trading_pair, size)
            if size > 0:
                buys.append(PriceSize(price, size))
        for ix, row in df_order_sell.iloc[:n_range].iterrows():
            price = Decimal(ix)
            size = row['size']
            size = market.quantize_order_amount(self.trading_pair, size)
            if size > 0:
                sells.append(PriceSize(price, size))
        return Proposal(buys, sells)

    def apply_order_levels_modifiers(self, proposal: Proposal):
        self.apply_price_band(proposal)

    def apply_price_band(self, proposal: Proposal):
        if self._price_ceiling > 0 and self.get_price() >= self._price_ceiling:
            proposal.buys = []
        if self._price_floor > 0 and self.get_price() <= self._price_floor:
            proposal.sells = []

    def apply_order_price_modifiers(self, proposal: Proposal):
        if self._order_optimization_enabled:
            self.apply_order_optimization(proposal)

    def apply_budget_constraint(self, proposal: Proposal):
        checker = self._market_info.market.budget_checker

        order_candidates = self.create_order_candidates_for_budget_check(proposal)
        adjusted_candidates = checker.adjust_candidates(order_candidates, all_or_none=True)
        self.apply_adjusted_order_candidates_to_proposal(adjusted_candidates, proposal)

    def create_order_candidates_for_budget_check(self, proposal: Proposal):
        order_candidates = []

        order_candidates.extend(
            [
                PerpetualOrderCandidate(
                    self.trading_pair,
                    OrderType.LIMIT,
                    TradeType.BUY,
                    buy.size,
                    buy.price,
                    leverage=Decimal(self._leverage),
                )
                for buy in proposal.buys
            ]
        )
        order_candidates.extend(
            [
                PerpetualOrderCandidate(
                    self.trading_pair,
                    OrderType.LIMIT,
                    TradeType.SELL,
                    sell.size,
                    sell.price,
                    leverage=Decimal(self._leverage),
                )
                for sell in proposal.sells
            ]
        )
        return order_candidates

    def apply_adjusted_order_candidates_to_proposal(self,
                                                    adjusted_candidates: List[PerpetualOrderCandidate],
                                                    proposal: Proposal):
        for order in chain(proposal.buys, proposal.sells):
            adjusted_candidate = adjusted_candidates.pop(0)
            if adjusted_candidate.amount == s_decimal_zero:
                self.logger().info(
                    f"Insufficient balance: {adjusted_candidate.order_side.name} order (price: {order.price},"
                    f" size: {order.size}) is omitted."
                )
                self.logger().warning(
                    "You are also at a possible risk of being liquidated if there happens to be an open loss.")
                order.size = s_decimal_zero
        proposal.buys = [o for o in proposal.buys if o.size > 0]
        proposal.sells = [o for o in proposal.sells if o.size > 0]

    def filter_limit(self, proposal: Proposal):
        limit = 18
        buys = []
        sells = []
        count = 0

        for p in proposal.sells:
            if count < limit:
                sells.append(p)
                count += 1
            else:
                self.logger().info(f"filter limit reached, stop adding orders... {p}")

        for p in proposal.buys:
            if count < limit:
                buys.append(p)
                count += 1
            else:
                self.logger().info(f"filter limit reached, stop adding orders... {p}")
        proposal = Proposal(buys, sells)
        return proposal

    def filter_out_takers(self, proposal: Proposal):
        market: ExchangeBase = self._market_info.market
        top_ask = market.get_price(self.trading_pair, True)
        if not top_ask.is_nan():
            proposal.buys = [buy for buy in proposal.buys if buy.price < top_ask]
        top_bid = market.get_price(self.trading_pair, False)
        if not top_bid.is_nan():
            proposal.sells = [sell for sell in proposal.sells if sell.price > top_bid]

    # Compare the market price with the top bid and top ask price
    def apply_order_optimization(self, proposal: Proposal):
        market: ExchangeBase = self._market_info.market
        own_buy_size = s_decimal_zero
        own_sell_size = s_decimal_zero

        # If there are multiple orders, do not jump prices
        if self._order_levels > 1:
            return

        for order in self.active_orders:
            if order.is_buy:
                own_buy_size = order.quantity
            else:
                own_sell_size = order.quantity

        if len(proposal.buys) == 1:
            # Get the top bid price in the market using order_optimization_depth and your buy order volume
            top_bid_price = self._market_info.get_price_for_volume(
                False, self._bid_order_optimization_depth + own_buy_size).result_price
            price_quantum = market.get_order_price_quantum(
                self.trading_pair,
                top_bid_price
            )
            # Get the price above the top bid
            price_above_bid = (ceil(top_bid_price / price_quantum) + 1) * price_quantum

            # If the price_above_bid is lower than the price suggested by the pricing proposal,
            # lower your price to this
            lower_buy_price = min(proposal.buys[0].price, price_above_bid)
            proposal.buys[0].price = market.quantize_order_price(self.trading_pair, lower_buy_price)

        if len(proposal.sells) == 1:
            # Get the top ask price in the market using order_optimization_depth and your sell order volume
            top_ask_price = self._market_info.get_price_for_volume(
                True, self._ask_order_optimization_depth + own_sell_size).result_price
            price_quantum = market.get_order_price_quantum(
                self.trading_pair,
                top_ask_price
            )
            # Get the price below the top ask
            price_below_ask = (floor(top_ask_price / price_quantum) - 1) * price_quantum

            # If the price_below_ask is higher than the price suggested by the pricing proposal,
            # increase your price to this
            higher_sell_price = max(proposal.sells[0].price, price_below_ask)
            proposal.sells[0].price = market.quantize_order_price(self.trading_pair, higher_sell_price)

    def did_fill_order(self, order_filled_event: OrderFilledEvent):
        order_id = order_filled_event.order_id
        market_info = self._sb_order_tracker.get_shadow_market_pair_from_order_id(order_id)

        if market_info is not None:
            if self._logging_options & self.OPTION_LOG_MAKER_ORDER_FILLED:
                self.log_with_clock(
                    logging.INFO,
                    f"({market_info.trading_pair}) Maker "
                    f"{'buy' if order_filled_event.trade_type is TradeType.BUY else 'sell'} order of "
                    f"{order_filled_event.amount} {market_info.base_asset} filled."
                )

    def did_complete_buy_order(self, order_completed_event: BuyOrderCompletedEvent):
        order_id = order_completed_event.order_id
        limit_order_record = self._sb_order_tracker.get_limit_order(self._market_info, order_id)
        if limit_order_record is None:
            return

        # delay order creation by filled_order_delay (in seconds)
        self._create_timestamp = self.current_timestamp + self._filled_order_delay
        self._cancel_timestamp = min(self._cancel_timestamp, self._create_timestamp)

        self._last_own_trade_price = limit_order_record.price

        self.log_with_clock(
            logging.INFO,
            f"({self.trading_pair}) Maker buy order {order_id} "
            f"({limit_order_record.quantity} {limit_order_record.base_currency} @ "
            f"{limit_order_record.price} {limit_order_record.quote_currency}) has been completely filled."
        )
        self.notify_hb_app_with_timestamp(
            f"Maker BUY  order {limit_order_record.quantity} {limit_order_record.base_currency} @ "
            f"{limit_order_record.price} {limit_order_record.quote_currency} is filled."
        )


    def did_complete_sell_order(self, order_completed_event: SellOrderCompletedEvent):
        order_id = order_completed_event.order_id
        limit_order_record: LimitOrder = self._sb_order_tracker.get_limit_order(self._market_info, order_id)
        if limit_order_record is None:
            return

        # delay order creation by filled_order_delay (in seconds)
        self._create_timestamp = self.current_timestamp + self._filled_order_delay
        self._cancel_timestamp = min(self._cancel_timestamp, self._create_timestamp)

        self._last_own_trade_price = limit_order_record.price

        self.log_with_clock(
            logging.INFO,
            f"({self.trading_pair}) Maker sell order {order_id} "
            f"({limit_order_record.quantity} {limit_order_record.base_currency} @ "
            f"{limit_order_record.price} {limit_order_record.quote_currency}) has been completely filled."
        )
        self.notify_hb_app_with_timestamp(
            f"Maker SELL order {limit_order_record.quantity} {limit_order_record.base_currency} @ "
            f"{limit_order_record.price} {limit_order_record.quote_currency} is filled."
        )
    def is_within_tolerance(self, current_prices: List[Decimal], proposal_prices: List[Decimal]) -> bool:
        if len(current_prices) != len(proposal_prices):
            return False
        current_prices = sorted(current_prices)
        proposal_prices = sorted(proposal_prices)
        for current, proposal in zip(current_prices, proposal_prices):
            # if spread diff is more than the tolerance or order quantities are different, return false.
            if abs(proposal - current) / current > self._order_refresh_tolerance_pct:
                return False
        return True

    # Return value: whether order cancellation is deferred.
    def cancel_active_orders(self, proposal: Proposal):
        if self._cancel_timestamp > self.current_timestamp:
            return

        to_defer_canceling = False
        if len(self.active_orders) == 0:
            return
        if proposal is not None and self._order_refresh_tolerance_pct >= 0:
            active_buy_prices = [Decimal(str(o.price)) for o in self.active_orders if o.is_buy]
            active_sell_prices = [Decimal(str(o.price)) for o in self.active_orders if not o.is_buy]
            proposal_buys = [buy.price for buy in proposal.buys]
            proposal_sells = [sell.price for sell in proposal.sells]
            if self.is_within_tolerance(active_buy_prices, proposal_buys) and \
                    self.is_within_tolerance(active_sell_prices, proposal_sells):
                to_defer_canceling = True

        if not to_defer_canceling:
            for order in self.active_orders:
                self.cancel_order(self._market_info, order.client_order_id)
        else:
            self.logger().info(f"Not cancelling active orders since difference between new order prices "
                               f"and current order prices is within "
                               f"{self._order_refresh_tolerance_pct:.2%} order_refresh_tolerance_pct")
            self.set_timers()

    def cancel_orders_below_min_spread(self):
        price = self.get_price()
        for order in self.active_orders:
            negation = -1 if order.is_buy else 1
            if (negation * (order.price - price) / price) < self._minimum_spread:
                self.logger().info(f"Order is below minimum spread ({self._minimum_spread})."
                                   f" Cancelling Order: ({'Buy' if order.is_buy else 'Sell'}) "
                                   f"ID - {order.client_order_id}")
                self.cancel_order(self._market_info, order.client_order_id)

    def to_create_orders(self, proposal: Proposal) -> bool:
        return (self._create_timestamp < self.current_timestamp and
                proposal is not None and
                len(self.active_orders) == 0)

    def execute_orders_proposal(self, proposal: Proposal, position_action: PositionAction):
        orders_created = False

        if len(proposal.buys) > 0:
            if position_action == PositionAction.CLOSE:
                if self.current_timestamp < self._next_buy_exit_order_timestamp:
                    return
                else:
                    self._next_buy_exit_order_timestamp = self.current_timestamp + self.filled_order_delay
            if self._logging_options & self.OPTION_LOG_CREATE_ORDER:
                price_quote_str = [f"{buy.size.normalize()} {self.base_asset}, "
                                   f"{buy.price.normalize()} {self.quote_asset}"
                                   for buy in proposal.buys]
                self.logger().info(
                    f"({self.trading_pair}) Creating {len(proposal.buys)} {self._close_order_type.name} bid orders "
                    f"at (Size, Price): {price_quote_str} to {position_action.name} position."
                )
            for buy in proposal.buys:
                bid_order_id = self.buy_with_specific_market(
                    self._market_info,
                    buy.size,
                    order_type=self._close_order_type,
                    price=buy.price,
                    position_action=position_action
                )
                if position_action == PositionAction.CLOSE:
                    self._exit_orders[bid_order_id] = self.current_timestamp
                orders_created = True
        if len(proposal.sells) > 0:
            if position_action == PositionAction.CLOSE:
                if self.current_timestamp < self._next_sell_exit_order_timestamp:
                    return
                else:
                    self._next_sell_exit_order_timestamp = self.current_timestamp + self.filled_order_delay
            if self._logging_options & self.OPTION_LOG_CREATE_ORDER:
                price_quote_str = [f"{sell.size.normalize()} {self.base_asset}, "
                                   f"{sell.price.normalize()} {self.quote_asset}"
                                   for sell in proposal.sells]
                self.logger().info(
                    f"({self.trading_pair}) Creating {len(proposal.sells)}  {self._close_order_type.name} ask "
                    f"orders at (Size, Price): {price_quote_str} to {position_action.name} position."
                )
            for sell in proposal.sells:
                ask_order_id = self.sell_with_specific_market(
                    self._market_info,
                    sell.size,
                    order_type=self._close_order_type,
                    price=sell.price,
                    position_action=position_action
                )
                if position_action == PositionAction.CLOSE:
                    self._exit_orders[ask_order_id] = self.current_timestamp
                orders_created = True
        if orders_created:
            self.set_timers()

    def set_timers(self):
        next_cycle = self.current_timestamp + self._order_refresh_time
        if self._create_timestamp <= self.current_timestamp:
            self._create_timestamp = next_cycle
        if self._cancel_timestamp <= self.current_timestamp:
            self._cancel_timestamp = min(self._create_timestamp, next_cycle)

    def notify_hb_app(self, msg: str):
        if self._hb_app_notification:
            super().notify_hb_app(msg)

    def get_price_type(self, price_type_str: str) -> PriceType:
        if price_type_str == "mid_price":
            return PriceType.MidPrice
        elif price_type_str == "best_bid":
            return PriceType.BestBid
        elif price_type_str == "best_ask":
            return PriceType.BestAsk
        elif price_type_str == "last_price":
            return PriceType.LastTrade
        elif price_type_str == 'last_own_trade_price':
            return PriceType.LastOwnTrade
        elif price_type_str == "custom":
            return PriceType.Custom
        else:
            raise ValueError(f"Unrecognized price type string {price_type_str}.")