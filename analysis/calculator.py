# file: analysis/calculator.py

import pandas as pd
import numpy as np
from dataclasses import dataclass
from datetime import timedelta

@dataclass
class BacktestResult:
    total_pnl: float
    total_trades: int
    win_rate: float
    avg_pnl: float
    sharpe_ratio: float
    max_drawdown: float
    daily_pnl: pd.Series = None
    equity_curve: pd.Series = None

class PerformanceCalculator:
    def __init__(self, trades: list, initial_capital: float, taker_fee: float = 0.0005):
        self.trades = trades
        self.initial_capital = initial_capital
        self.taker_fee = taker_fee # 简单起见，回测暂时全按 Taker 算

    def calculate(self) -> BacktestResult:
        if not self.trades:
            return BacktestResult(0, 0, 0, 0, 0, 0)

        # 1. 将成交记录转为 DataFrame
        df = pd.DataFrame([vars(t) for t in self.trades])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df = df.sort_values('datetime')

        # 2. 计算每笔盈亏 (PnL)
        # 简化逻辑：每次 Open 和 Close 视为独立事件，这里我们通过基于 Trade 的 PnL 估算
        # 更精确的方法是基于 Round-Trip (开平仓配对)，但对于高频流式数据，
        # 我们采用 "逐笔市值变动 + 手续费" 的方式有点复杂，
        # 这里采用简化的 "单笔成交额 * 价格差" 逻辑很难，
        # 所以我们改用：基于每日/每分钟的 Net Value (净值) 序列来计算。
        
        # 为了 Step 7 演示，我们使用一个更通用的方法：
        # 我们假设外部传入的是 "资金曲线 (Equity Curve)"，而不是原始成交单。
        # 因此，我们需要修改 BacktestEngine 来记录每次成交后的余额变化。
        raise NotImplementedError("请使用 calculate_from_equity 方法")

    def calculate_from_equity(self, equity_list: list) -> BacktestResult:
        """
        基于资金曲线计算指标
        equity_list: List[{'datetime': ..., 'balance': ...}]
        """
        if not equity_list:
            return BacktestResult(0, 0, 0, 0, 0, 0)

        df = pd.DataFrame(equity_list)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        
        # 1. 收益率
        # 计算每笔变动的收益率 (近似)
        df['return'] = df['balance'].pct_change().fillna(0)
        
        # 2. 总盈亏
        end_balance = df['balance'].iloc[-1]
        total_pnl = end_balance - self.initial_capital
        
        # 3. 最大回撤
        df['cummax'] = df['balance'].cummax()
        df['drawdown'] = (df['balance'] - df['cummax']) / df['cummax']
        max_dd = df['drawdown'].min()
        
        # 4. 夏普比率 (假设数据是按 Tick 记录的，这很不准，通常按日计算)
        # 这里我们需要重采样到 "小时" 或 "天"
        df_daily = df['balance'].resample('1h').last().ffill() # 重采样为小时数据
        daily_returns = df_daily.pct_change().fillna(0)
        
        if daily_returns.std() == 0:
            sharpe = 0
        else:
            # 假设一年 24*365 小时，无风险利率 0
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(24 * 365)

        # 5. 简单的胜率统计 (需要原始 Trade 列表才能精确计算，这里略过或需传入 Trades)
        # 我们简单统计一下 equity 增长的次数占比（不准确，仅演示）
        win_ticks = len(df[df['return'] > 0])
        total_ticks = len(df[df['return'] != 0])
        win_rate = win_ticks / total_ticks if total_ticks > 0 else 0

        return BacktestResult(
            total_pnl=total_pnl,
            total_trades=total_ticks, # 这里近似为净值变动次数
            win_rate=win_rate,
            avg_pnl=total_pnl / total_ticks if total_ticks > 0 else 0,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            equity_curve=df['balance']
        )