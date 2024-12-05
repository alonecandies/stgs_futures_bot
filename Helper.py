from concurrent.futures import ThreadPoolExecutor, as_completed
from binance.client import Client

import BotClass
from LiveTradingConfig import *
from binance import ThreadedWebsocketManager
import time
from Logger import *
from typing import List
import numpy as np
import os
import sys

def convert_buffer_to_string(buffer_int):
    ''' Function that converts the candle number to a String in hours/days to pass to binance'''
    try:
        unit_of_time = interval[-1]
        unit_in_minutes: int
        match unit_of_time:
            case 'm':
                unit_in_minutes = int(interval[:-1])
            case 'h':
                unit_in_minutes = int(interval[:-1]) * 60
            case 'd':
                unit_in_minutes = int(interval[:-1]) * 1440
            case _:
                unit_in_minutes = 1

        number_of_minutes_required = unit_in_minutes * buffer_int
        hours_required = int(np.ceil(number_of_minutes_required / 60))
        if hours_required < 24:
            log.info(f'convert_buffer_to_string() - required buffer calculated is {hours_required} hours ago')
            return f'{hours_required} hours ago'
        else:
            days_required = int(np.ceil(hours_required / 24))
            log.info(f'convert_buffer_to_string() - required buffer calculated is {days_required} days ago')
            return f'{days_required} days ago'
    except Exception as e:
        exc_type, exc_obj, exc_tb = sys.exc_info()
        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
        log.warning(f"convert_buffer_to_string() - Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")

class CustomClient:
    def __init__(self, client: Client):
        self.client = client
        self.leverage = leverage
        self.twm = ThreadedWebsocketManager(api_key=API_KEY, api_secret=API_SECRET)
        self.number_of_bots = 0

    def set_leverage(self, symbols_to_trade: List[str]):
        ''' Function that sets the leverage for each coin as specified in live_trading_config.py '''
        log.info("set_leverage() - Setting Leverage in parallel batches...")
        
        def set_symbol_leverage(symbol):
            """Helper function to set leverage for a single symbol."""
            try:
                self.client.futures_change_leverage(symbol=symbol, leverage=self.leverage)
                log.info(f"set_leverage() - Successfully set leverage on {symbol}")
                return symbol, None
            except Exception as e:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                log.warning(f"set_leverage() - Symbol: {symbol} failed due to error, "
                            f"Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")
                return symbol, e

        # Use ThreadPoolExecutor to run tasks in parallel
        max_workers = min(10, len(symbols_to_trade))  # Adjust the number of workers as needed
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(set_symbol_leverage, symbol): symbol for symbol in symbols_to_trade}
            results = []
            
            for future in as_completed(futures):
                symbol, error = future.result()
                if error:
                    symbols_to_trade.remove(symbol)  # Remove failed symbol from the list
                else:
                    results.append(symbol)

        log.info(f"set_leverage() - Completed leverage setting. Successfully set for {len(results)} symbols.")

    def start_websockets(self, bots: List[BotClass.Bot]):
        ''' Function that starts the websockets for price data in the bots '''
        self.twm.start()  ##start ws manager
        log.info("start_websockets() - Starting Websockets...")
        i = 0
        while i < len(bots):
            try:
                bots[i].stream = self.twm.start_kline_futures_socket(callback=bots[i].handle_socket_message,
                                                                     symbol=bots[i].symbol, interval=interval)
                i += 1
            except Exception as e:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                log.warning(f"start_websockets() - Symbol: {bots[i].symbol}, Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")
                bots.pop(i)
        self.number_of_bots = len(bots)

    def ping_server_reconnect_sockets(self, bots: List[BotClass.Bot]):
        ''' Loop that runs constantly, it pings the server every 15 seconds, so we don't lose connection '''
        while True:
            time.sleep(15)
            self.client.futures_ping()
            for bot in bots:
                if bot.socket_failed:
                    try:
                        log.info(f"retry_websockets_job() - Attempting to reset socket for {bot.symbol}")
                        self.twm.stop_socket(bot.stream)
                        bot.stream = self.twm.start_kline_futures_socket(bot.handle_socket_message, symbol=bot.symbol)
                        bot.socket_failed = False
                        log.info(f"retry_websockets_job() - Reset successful")
                    except Exception as e:
                        exc_type, exc_obj, exc_tb = sys.exc_info()
                        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                        log.error(f"retry_websockets_job() - Error in resetting websocket for {bot.symbol}, Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")

    def setup_bots(self, bots: List[BotClass.Bot], symbols_to_trade: List[str], signal_queue, print_trades_q):
        ''' Function that initializes a Bot class for each symbol in our symbols_to_trade list / All symbols if trade_all_coins is True '''
        log.info(f"setup_bots() - Beginning Bots setup...")
        y = self.client.futures_exchange_info()['symbols']
        symbol_info = [[x['pair'], x['pricePrecision'], x['quantityPrecision'], x['filters'][0]['tickSize']] for x in y]

        i = 0
        while i < len(symbols_to_trade):
            symbol_precision = -99
            order_precision = -99
            tick = -99
            flag = 0
            for x in symbol_info:
                if x[0] == symbols_to_trade[i]:
                    symbol_precision = int(x[1])
                    order_precision = int(x[2])
                    tick = float(x[3])
                    flag = 1
                    break
            if flag == 1:
                bots.append(
                    BotClass.Bot(symbol=symbols_to_trade[i], Open=[], Close=[], High=[], Low=[], Volume=[], Date=[],
                                  OP=order_precision, CP=symbol_precision,
                                  index=i, tick=tick, strategy=trading_strategy, TP_SL_choice=TP_SL_choice,
                                  SL_mult=SL_mult, TP_mult=TP_mult, signal_queue=signal_queue, print_trades_q=print_trades_q))
                i += 1
            else:
                log.info(f"setup_bots() - {symbols_to_trade[i]} no symbol info found, removing symbol")
                try:
                    symbols_to_trade.pop(i)
                except Exception as e:
                    exc_type, exc_obj, exc_tb = sys.exc_info()
                    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                    log.warning(f"setup_bots() - Error occurred removing symbol, Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")
        log.info(f"setup_bots() - Bots have completed setup")

    def combine_data(self, bots: List[BotClass.Bot], symbols_to_trade: List[str], buffer):
        ''' Function that pulls in historical data so we have candles for the Bot to start trading immediately '''
        log.info("combine_data() - Combining Historical and WebSocket data in parallel batches...")

        def process_bot(bot, buffer):
            """Helper function to process historical data for a single bot."""
            try:
                log.info(f"combine_data() - Gathering and combining data for {bot.symbol}...")
                date_temp, open_temp, close_temp, high_temp, low_temp, volume_temp = self.get_historical(
                    symbol=bot.symbol, buffer=buffer)
                
                # Pop off last candle as it is a duplicate
                date_temp.pop(-1)
                open_temp.pop(-1)
                close_temp.pop(-1)
                high_temp.pop(-1)
                low_temp.pop(-1)
                volume_temp.pop(-1)
                
                # Add historical data to bot
                bot.add_hist(
                    Date_temp=date_temp,
                    Open_temp=open_temp,
                    Close_temp=close_temp,
                    High_temp=high_temp,
                    Low_temp=low_temp,
                    Volume_temp=volume_temp
                )
                return bot, None
            except Exception as e:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                log.warning(f"combine_data() - Error occurred adding data for {bot.symbol}, "
                            f"Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")
                return bot, e

        max_workers = min(10, len(bots))  # Adjust max_workers as necessary
        failed_bots = []

        # Process bots in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_bot, bot, buffer): bot for bot in bots}
            for future in as_completed(futures):
                bot, error = future.result()
                if error:
                    # Stop the WebSocket and remove bot and symbol if processing failed
                    try:
                        self.twm.stop_socket(bot.stream)
                        symbols_to_trade.remove(bot.symbol)
                        bots.remove(bot)
                        self.number_of_bots -= 1
                    except Exception as e:
                        exc_type, exc_obj, exc_tb = sys.exc_info()
                        fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                        log.warning(f"combine_data() - Error occurred removing symbol {bot.symbol}, "
                                    f"Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}")
                    failed_bots.append(bot.symbol)

        if failed_bots:
            log.info(f"combine_data() - Failed to combine data for symbols: {failed_bots}")
        log.info("combine_data() - Finished combining data for all symbols. Searching for trades now...")

    def get_historical(self, symbol: str, buffer):
        ''' Function that pulls the historical data for a symbol '''
        Open = []
        High = []
        Low = []
        Close = []
        Volume = []
        Date = []
        try:
            for kline in self.client.futures_historical_klines(symbol, interval, start_str=buffer):
                Date.append(int(kline[6]))
                Open.append(float(kline[1]))
                Close.append(float(kline[4]))
                High.append(float(kline[2]))
                Low.append(float(kline[3]))
                Volume.append(float(kline[7]))
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            log.error(
                f'get_historical() - Error occurred for symbol: {symbol}, Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}')
        return Date, Open, Close, High, Low, Volume

    def get_account_balance(self):
        ''' Function that returns the USDT balance of the account '''
        try:
            account_balance_info = self.client.futures_account_balance()
            for x in account_balance_info:
                if x['asset'] == 'USDT':
                    return float(x['balance'])
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            log.error(f'get_account_balance() - error getting account balance, Error Info: {exc_obj, fname, exc_tb.tb_lineno}, Error: {e}')


class Trade:
    def __init__(self, index: int, entry_price: float, position_size: float, take_profit_val: float,
                 stop_loss_val: float, trade_direction: int, order_id: int, symbol: str, CP: int, tick_size: float):
        self.index = index
        self.symbol = symbol
        self.entry_price = entry_price
        self.position_size = position_size
        if trade_direction:
            self.TP_val = entry_price + take_profit_val
            self.SL_val = entry_price - stop_loss_val
        else:
            self.TP_val = entry_price - take_profit_val
            self.SL_val = entry_price + stop_loss_val
        self.CP = CP
        self.tick_size = tick_size
        self.trade_direction = trade_direction
        self.order_id = order_id
        self.TP_id = ''
        self.SL_id = ''
        self.trade_status = 0  ## hasn't started
        self.trade_start = ''
        self.Highest_val = -9_999_999
        self.Lowest_val = 9_999_999
        self.trail_activated = False
        self.same_candle = True
        self.current_price = 0
