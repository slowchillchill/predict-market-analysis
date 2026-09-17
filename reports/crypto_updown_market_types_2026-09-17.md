# Polymarket 与 Kalshi 加密货币 Up/Down 市场种类

整理日期：2026-09-17。本文记录本次讨论的市场分类结论，作为后续数据采集与跨平台比较的范围参考。

## 核心结论

| 平台 | 已确认有在开盘市场的币种数 | 周期种类数 | 币种 × 周期组合数 |
| --- | ---: | ---: | ---: |
| Polymarket | 8 | 5 | **38** |
| Kalshi | 9 | 1 | **9** |

- **Polymarket**：BTC、ETH、SOL、XRP、DOGE、BNB、HYPE 覆盖 5 分钟、15 分钟、1 小时、4 小时、日线；ZEC 覆盖 5 分钟、15 分钟、4 小时。合计 `7 × 5 + 3 = 38` 种组合。
- **Kalshi**：确认在开盘的单币种涨跌方向系列均为 **15 分钟**，覆盖 BTC、ETH、SOL、XRP、DOGE、BNB、HYPE、ZEC、NEAR，共 9 种组合。
- 两个平台共同覆盖 **8 个币种的 15 分钟 Up/Down**，可按同币种、同周期建立对应关系。
- 以上是调查当日的系列覆盖情况；平台后续可能新增、暂停或更换系列。

## 统计口径

这里的 Up/Down 指：比较同一币种在一个时间区间开始与结束时的价格，判断区间涨跌方向。

“市场种类”按**币种 × 周期**计数。例如 BTC 5 分钟和 BTC 15 分钟算两种；同一天连续生成的 288 个 BTC 5 分钟市场仍属于同一种。
Up 和 Down 是同一市场的两个方向，不分开计数。

以下产品不计入本表：固定价格以上／以下、价格区间、期间触及某价、年度最高／最低、币种之间的收益率竞赛等。
系列名称含 `Directional`、页面显示 Up/Down，或到期周期相同，都不足以证明结算问题相同。

## Polymarket：8 个币种、5 种周期、38 种组合

“✓”表示本次确认有在开盘市场；“—”表示本次未确认，不表示历史上或未来一定不存在。

| 币种 | 5 分钟 | 15 分钟 | 1 小时 | 4 小时 | 日线 | 组合数 |
| --- | --- | --- | --- | --- | --- | ---: |
| BTC | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| ETH | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| SOL | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| XRP | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| DOGE | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| BNB | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| HYPE | ✓ | ✓ | ✓ | ✓ | ✓ | 5 |
| ZEC | ✓ | ✓ | — | ✓ | — | 3 |
| **合计** | **8** | **8** | **7** | **8** | **7** | **38** |

对应的当前系列标识如下。应使用实际 `series_slug`，不要仅凭币种缩写拼接所有名称：

| 币种 | 5 分钟 | 15 分钟 | 1 小时 | 4 小时 | 日线 |
| --- | --- | --- | --- | --- | --- |
| BTC | `btc-up-or-down-5m` | `btc-up-or-down-15m` | `btc-up-or-down-hourly` | `btc-up-or-down-4h` | `btc-up-or-down-daily` |
| ETH | `eth-up-or-down-5m` | `eth-up-or-down-15m` | `eth-up-or-down-hourly` | `eth-up-or-down-4h` | `eth-up-or-down-daily` |
| SOL | `sol-up-or-down-5m` | `sol-up-or-down-15m` | `solana-up-or-down-hourly` | `sol-up-or-down-4h` | `solana-up-or-down-daily` |
| XRP | `xrp-up-or-down-5m` | `xrp-up-or-down-15m` | `xrp-up-or-down-hourly` | `xrp-up-or-down-4h` | `xrp-up-or-down-daily` |
| DOGE | `doge-up-or-down-5m` | `doge-up-or-down-15m` | `doge-up-or-down-hourly` | `doge-up-or-down-4h` | `dogecoin-up-or-down-daily` |
| BNB | `bnb-up-or-down-5m` | `bnb-up-or-down-15m` | `bnb-up-or-down-hourly` | `bnb-up-or-down-4h` | `bnb-up-or-down-daily` |
| HYPE | `hype-up-or-down-5m` | `hype-up-or-down-15m` | `hype-up-or-down-hourly` | `hype-up-or-down-4h` | `hype-up-or-down-daily` |
| ZEC | `zec-up-or-down-5m` | `zec-up-or-down-15m` | — | `zec-up-or-down-4h` | — |

BTC、ETH 的周线和月线系列能找到历史记录，但本次没有确认在开盘市场，因此不计入当前 38 种组合。
同样，旧的 BTC 小时系列、部分旧 4 小时系列，以及旧 HYPE／FARTCOIN 日线记录，不应重复计入当前覆盖。

参考：[Polymarket 加密货币市场](https://polymarket.com/crypto)；本次结论结合 Gamma 系列清单及系列下的实际事件确认。

## Kalshi：9 个币种，均为 15 分钟

| 币种 | 周期 | 系列标识 |
| --- | --- | --- |
| BTC | 15 分钟 | `KXBTC15M` |
| ETH | 15 分钟 | `KXETH15M` |
| SOL | 15 分钟 | `KXSOL15M` |
| XRP | 15 分钟 | `KXXRP15M` |
| DOGE | 15 分钟 | `KXDOGE15M` |
| BNB | 15 分钟 | `KXBNB15M` |
| HYPE | 15 分钟 | `KXHYPE15M` |
| ZEC | 15 分钟 | `KXZEC15M` |
| NEAR | 15 分钟 | `KXNEAR15M` |

以上系列均确认有开盘市场，样本的结算规则比较相隔 15 分钟的起止参考价格。
系列目录另有 `KXADA15M`、`KXBCH15M`、`KXTON15M`，但本次未返回开盘市场，故不计入上表的 9 种；仅凭目录存在不能断言当前在开盘，也不能断言永久停用。

需要区分的相邻产品：

- `KXBTCD`、`KXETHD`、`KXSOLD`、`KXBNBD`、`KXHYPED` 等样本比较到期价格与固定目标价，不能当作相同周期的区间涨跌系列。
- `KXCRYPTOLEAD15M` 是多币种收益率竞赛；`KXCRYPTOCOMP15M` 属于币种收益比较产品，不计入单币种 Up/Down。
- 本次没有确认在开盘且符合上述区间涨跌定义的 Kalshi 5 分钟、1 小时、4 小时或日线系列。

参考：[Kalshi 加密货币系列目录](https://external-api.kalshi.com/trade-api/v2/series?category=Crypto)、
[BTC 15 分钟开盘市场查询](https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1)。
分类同时检查系列目录与市场结算规则；接口含义见 [系列查询文档](https://docs.kalshi.com/api-reference/market/get-series-list) 和 [市场查询文档](https://docs.kalshi.com/api-reference/market/get-markets)。

## 跨平台对应关系

共同的 15 分钟币种为 **BTC、ETH、SOL、XRP、DOGE、BNB、HYPE、ZEC**，共 8 对系列。
NEAR 的 15 分钟系列本次仅在 Kalshi 确认；Polymarket 另外覆盖上述表格列出的其他周期。

同币种、同周期可以作为分类与研究的对照范围，但不等于两个平台的合约完全相同。
后续比较价格或交易表现时，仍需按具体市场核对价格来源、取价窗口、时间边界和持平时的结算规则。

本次完成的是市场种类盘点。现有逐笔采集功能只覆盖 **Polymarket BTC 5 分钟**，不能把本表的系列覆盖范围理解为已经下载了全部币种与周期的数据。
