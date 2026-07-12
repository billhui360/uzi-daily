# UZI 每日选股邮件（云端）

每个交易日收盘后，GitHub 云端自动跑一遍选股，把结果发你邮箱。**你电脑不用开。**

## 策略

- **范围**：中证500 + 中证1000 成分股（≈1500 中盘），数据走 baostock（海外服务器也稳）
- **入场**：月线上升趋势 Stage2 —— 来自大底 + 走出底部 + 月线多头 + 低点抬高 + 量能配合
- **增强**（经 UZI 回测样本外验证）：
  - 🧩 **筹码集中**：股东户数较 2 季前下降（主力吸筹）→ 加分/标注
  - 🌡️ **大盘择时**：沪深300 跌破 MA10 → 邮件顶部红色警示，建议空仓
  - 🥇 **③逼近前高优先**：回测里该阶段表现最好，榜单按此排序
- 另出：**自选股每日体检**（编辑 `watchlist.txt`）

> ⚠️ 诚实说：回测证明选股 alpha 很薄，真实价值在大盘择时与筹码信号。当研究助手用，别闭眼跟。

## 部署（一次性）

1. 把本仓库推到你的 GitHub 账号（见下方"推送"）。
2. 到仓库 **Settings → Secrets and variables → Actions → New repository secret**，加这几条：

   | Secret | 填什么 | 例 |
   |---|---|---|
   | `SMTP_HOST` | 邮箱服务器 | `smtp.qq.com` |
   | `SMTP_PORT` | 端口(SSL) | `465` |
   | `SMTP_USER` | 发件邮箱 | `你@qq.com` |
   | `SMTP_PASS` | **邮箱授权码**(不是登录密码) | — |
   | `MAIL_TO` | 收件人,逗号分隔 | `你@qq.com` |
   | `MAIL_FROM` | 发件人(可选,默认=USER) | — |

3. 到 **Actions** 页，点「UZI 每日选股邮件」→ **Run workflow** 手动跑一次,确认能收到邮件。之后每个交易日 16:30 自动跑。

## 改配置

- 关注的票：编辑 `watchlist.txt`
- 榜单条数：workflow 里 `TOP_N`
- 定时：workflow 里 `cron`（UTC 时间）

## 本地测试（不发信）

```bash
pip install -r requirements.txt
DRY_RUN=1 python3 daily_report.py   # 生成 last_report.html
```
