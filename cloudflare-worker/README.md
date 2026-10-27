# Cloudflare Worker 定时触发器

## 触发链路

```
Cloudflare Cron  1 * * * *（UTC）
        │
        ▼
Worker.scheduled()
        │  POST /repos/InterestM/ocoR/actions/workflows/sync.yml/dispatches
        ▼
GitHub Actions：workflow_dispatch → sync job
```

本 Worker 是 `sync.yml` 唯一的定时入口，工作流仅保留 `workflow_dispatch`。

## 部署

前置条件：

- Cloudflare 账号（Free 计划即可）；
- GitHub fine-grained PAT，仅授权 `InterestM/ocoR`，权限为 `Actions: Read and write`。

```bash
cd cloudflare-worker
npm install
npx wrangler login
npx wrangler secret put GITHUB_TOKEN      # 上一步的 PAT
npx wrangler secret put TRIGGER_SECRET    # 可选，用于 HTTP 手动触发
npx wrangler deploy
```

部署后在 Dashboard → Workers & Pages → `mltd-sync-trigger` → Settings → Trigger Events
确认 Cron `1 * * * *` 已注册。

## 验证

| 场景 | 操作 |
| --- | --- |
| 线上定时 | 等待整点后检查 [Actions 运行时](https://github.com/InterestM/ocoR/actions) 是否有 `workflow_dispatch` 记录 |
| 线上手动 | `curl -X POST -H "Authorization: Bearer $TRIGGER_SECRET" https://mltd-sync-trigger.<子域>.workers.dev/trigger` |
| 本地模拟 | `npx wrangler dev --test-scheduled`，随后请求 `http://localhost:8787/__scheduled?cron=1+*+*+*+*` |
| 日志 | `npx wrangler tail` |

本地调试需将 `.dev.vars.example` 复制为 `.dev.vars` 并填入 token；该文件已被 `.gitignore`
排除。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| Cron | — | 由 Cloudflare 调度，失败时抛错以标记本次执行失败 |
| POST | `/trigger` | 手动触发；需 `Authorization: Bearer <TRIGGER_SECRET>` 或 `?token=` |
| GET | `/health` | 健康检查，返回服务名与当前时间 |

未配置 `TRIGGER_SECRET` 时 `/trigger` 返回 404。

## 运维说明

**时区**：Cron 表达式按 UTC 解析，`1 * * * *` 即每小时第 1 分钟（北京时间 `HH:01`）。

**延迟**：Cloudflare Cron 为 best-effort 投递，延迟数秒至数分钟属正常范围；dispatch 被
受理后，GitHub 起 runner 另有数秒至数十秒排队时间。

**重试策略**：`401/403/404/422` 不重试；`5xx` 与网络异常最多重试 3 次，退避间隔 2s、4s。
失败信息写入 `wrangler tail` 与 Dashboard 日志。

**并发与幂等**：工作流配置 `concurrency: mltd-sync`，重复触发排队执行；manifest 无变化时
不提交。

**额度**：Free 计划支持 5 条 Cron Trigger、每日 10 万次请求。

**凭据轮换**：`GITHUB_TOKEN` 仅以 secret 形式存储。泄露后在 GitHub 吊销并重新生成，执行
`npx wrangler secret put GITHUB_TOKEN` 覆盖。

**错误码**：404 = 工作流在目标 ref 上不存在或被禁用；422 = `ref` 无效。两者均不重试。
