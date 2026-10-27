/**
 * Cloudflare Worker：按 Cron 触发本仓库的 GitHub Actions 工作流。
 *
 * 触发方式（二选一）：
 *   1. Cron Trigger：wrangler.toml 里的 ["1 * * * *"]，每小时第 1 分钟（UTC）。
 *   2. 手动 HTTP：GET/POST /trigger，带 `Authorization: Bearer <TRIGGER_SECRET>`
 *      或 `?token=<TRIGGER_SECRET>`，方便临时补跑或验证配置。
 *
 * 必须配置的 secret：
 *   GITHUB_TOKEN    细粒度 PAT，仓库 InterestM/ocoR，权限 Actions: Read and write
 * 可选 secret：
 *   TRIGGER_SECRET  不配置时 HTTP 触发接口关闭（返回 404），只保留 Cron 触发
 */

const API_VERSION = "2022-11-28";

const DEFAULTS = {
  repo: "InterestM/ocoR",
  workflow: "sync.yml",
  ref: "main",
};

/**
 * 调用 GitHub API 触发 workflow_dispatch，带简单重试。
 * @returns {Promise<{ok: boolean, status: number, detail: string}>}
 */
async function dispatchWorkflow(env, source) {
  const token = env.GITHUB_TOKEN;
  if (!token) {
    throw new Error("缺少 GITHUB_TOKEN secret，请先执行 `wrangler secret put GITHUB_TOKEN`");
  }

  const repo = env.GITHUB_REPO || DEFAULTS.repo;
  const workflow = env.GITHUB_WORKFLOW || DEFAULTS.workflow;
  const ref = env.GITHUB_REF || DEFAULTS.ref;
  const url = `https://api.github.com/repos/${repo}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;

  // 注意：ref 必须指向包含该 workflow 文件的分支，通常是默认分支。
  const payload = JSON.stringify({ ref, inputs: {} });

  const attempts = 3;
  let last = { ok: false, status: 0, detail: "not attempted" };

  for (let i = 1; i <= attempts; i++) {
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": API_VERSION,
          "Content-Type": "application/json",
          // GitHub API 要求非空 User-Agent，Workers 运行时并不保证会带上。
          "User-Agent": "mltd-sync-trigger-worker",
        },
        body: payload,
      });

      // 204 = 已受理；422 一般是 ref 不存在，404 一般是 workflow 不存在/被禁用，重试无意义。
      if (resp.status === 204) {
        console.log(`[${source}] workflow_dispatch 已受理：${repo}/${workflow}@${ref}（第 ${i} 次尝试）`);
        return { ok: true, status: 204, detail: "accepted" };
      }

      const body = await resp.text();
      last = { ok: false, status: resp.status, detail: body.slice(0, 500) };
      console.error(`[${source}] workflow_dispatch 失败：HTTP ${resp.status} ${last.detail}`);

      if (resp.status === 401 || resp.status === 403 || resp.status === 404 || resp.status === 422) {
        break; // 认证/参数问题，重试无意义
      }
    } catch (err) {
      last = { ok: false, status: 0, detail: String(err && err.stack ? err.stack : err) };
      console.error(`[${source}] workflow_dispatch 请求异常（第 ${i} 次尝试）：${last.detail}`);
    }

    if (i < attempts) {
      await sleep(2000 * i);
    }
  }

  return last;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 恒定时间比较，避免通过响应时间猜 secret。 */
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2) + "\n", {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export default {
  /** Cron Trigger 入口 */
  async scheduled(controller, env, _ctx) {
    const startedAt = new Date(controller.scheduledTime).toISOString();
    const result = await dispatchWorkflow(env, `cron ${startedAt}`);
    if (!result.ok) {
      // 抛错会让这次 Cron 执行在 Dashboard/日志里标记为失败，便于告警。
      throw new Error(`触发失败：HTTP ${result.status} ${result.detail}`);
    }
  },

  /** HTTP 入口，仅用于手动补跑与健康检查 */
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return json({ ok: true, service: "mltd-sync-trigger", time: new Date().toISOString() });
    }

    if (url.pathname !== "/trigger" && url.pathname !== "/") {
      return json({ error: "not found" }, 404);
    }

    const secret = env.TRIGGER_SECRET;
    if (!secret) {
      // 未配置 secret 时关闭 HTTP 触发，避免任何人都能消耗 Actions 额度。
      return json({ error: "HTTP trigger disabled" }, 404);
    }

    const auth = request.headers.get("authorization") || "";
    const bearer = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
    const provided = bearer || url.searchParams.get("token") || "";
    if (!safeEqual(provided, secret)) {
      return json({ error: "unauthorized" }, 401);
    }

    const result = await dispatchWorkflow(env, "http");
    return json(result, result.ok ? 202 : 502);
  },
};
