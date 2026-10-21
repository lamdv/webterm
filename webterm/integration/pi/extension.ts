/**
 * Webterm's pi integration: prompt context and sub-agent tools.
 *
 * Webterm injects this extension into every pi process it starts. It teaches the
 * model that it is running inside webterm (see `before_agent_start` below) and
 * exposes the sub-agent tools, which call the local `webtermctl` control socket.
 * That client fills in the parent session id from WEBTERM_SESSION_ID, so child
 * agents appear under the current session in the browser tree.
 */

import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type ExecResult = { stdout: string; stderr: string; code: number };

/**
 * Prompt context for the sessions webterm owns.
 *
 * It reaches the model as a named system-prompt section set from
 * `before_agent_start`, so it exists only in pi processes webterm launched with
 * `-e <this file>`. A plain `pi` in a terminal never loads it, and nothing is
 * written to a shared `APPEND_SYSTEM.md`.
 *
 * Keep this text static. Pi records prompt sections in the transcript and sends
 * a delta when one changes, so interpolating anything per-turn (session ids,
 * child counts, depth) would rewrite the prompt prefix on every request and
 * invalidate the provider's cache. Volatile facts belong in tool results.
 */
const WEBTERM_CONTEXT = `You are running inside webterm, a browser terminal multiplexer. The human watches
this session live, and every session you create appears as a child tab they can
open, watch, or take over.

Control is on PATH as \`webtermctl\` ($WEBTERM_SOCKET, $WEBTERM_SESSION_ID and
$WEBTERM_DEPTH are set for this process). The webterm tools wrap it: spawn_agent,
agent_send, agent_wait, agent_result, agent_list, and agent_kill.

- Delegate work that is separable, long-running, or would flood this transcript
  with output; do work whose result is a few lines yourself.
- A sub-agent sees only the task text you give it, never this conversation.
- Children are attributed to you and visible to the human; keep task labels short.
- Pick a child's model with spawn_agent's optional model (and provider) fields
  instead of dropping to webtermctl; a profile such as llama may be available.
- Depth is capped, so do not recurse without a reason ($WEBTERM_DEPTH is yours).
- Read a child's answer with agent_result instead of asking the human to relay it.`;

/** One-line reminder for the `/webterm` command. */
const WEBTERM_SUMMARY =
	"webterm session pool: webtermctl on PATH plus spawn_agent/agent_send/agent_wait/agent_result/agent_list/agent_kill. Children appear as tabs under this session.";

function errorText(result: ExecResult): string {
	const stderr = result.stderr.trim();
	const stdout = result.stdout.trim();
	return stderr || stdout || `webtermctl exited with code ${result.code}`;
}

async function ctl(
	pi: ExtensionAPI,
	args: string[],
	timeout = 120_000,
): Promise<Record<string, unknown>> {
	const result = (await pi.exec("webtermctl", ["--json", ...args], {
		timeout,
	})) as ExecResult;
	if (result.code !== 0) throw new Error(errorText(result));
	const text = result.stdout.trim();
	if (!text) return {};
	try {
		return JSON.parse(text) as Record<string, unknown>;
	} catch {
		return { text };
	}
}

function jsonContent(value: unknown): { type: "text"; text: string }[] {
	return [{ type: "text", text: JSON.stringify(value, null, 2) }];
}

export default function (pi: ExtensionAPI) {
	pi.on("before_agent_start", (event) => {
		const options = event.systemPromptOptions;
		// Only claim control that exists: an extension load with the tools hidden
		// (or a host that injects this file for another reason) stays silent.
		if (!options.selectedTools.includes("spawn_agent")) {
			delete options.sections.webterm;
			return;
		}
		options.sections.webterm = WEBTERM_CONTEXT;
	});

	pi.registerCommand("webterm", {
		description: "Explain the webterm session pool and sub-agent tools",
		handler: async (_args, ctx) => {
			ctx.ui.notify(WEBTERM_SUMMARY, "info");
		},
	});

	pi.registerTool({
		name: "spawn_agent",
		label: "Spawn sub-agent",
		description:
			"Start a child agent for a task. The child appears under this session in webterm and can be messaged, waited on, or killed.",
		parameters: Type.Object({
			task: Type.String({ description: "The task for the child agent." }),
			name: Type.Optional(
				Type.String({ description: "Short display name for the child session." }),
			),
			agent: Type.Optional(
				Type.String({
					description: "Webterm agent profile name. Defaults to pi.",
				}),
			),
			model: Type.Optional(
				Type.String({
					description:
						"Model for the child, as pi resolves it (id, provider/id, or provider/id:thinking).",
				}),
			),
			provider: Type.Optional(
				Type.String({
					description: "Provider for model. Requires model.",
				}),
			),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
			const agent = params.agent || "pi";
			const title = params.name || params.task.slice(0, 80);
			const args = [
				"spawn",
				"--agent",
				agent,
				"--rpc",
				"--task",
				params.task,
				"--title",
				title,
				"--no-wait",
			];
			if (params.provider) args.push("--provider", params.provider);
			if (params.model) args.push("--model", params.model);
			const result = await ctl(pi, args, 30_000);
			const sessionId = String(result.session_id || "");
			const created = {
				session_id: sessionId,
				agent,
				task: params.task,
				model: params.model,
				provider: params.provider,
			};
			return {
				content: jsonContent(created),
				details: created,
			};
		},
	});

	pi.registerTool({
		name: "agent_send",
		label: "Message sub-agent",
		description:
			"Send a prompt, steering message, or follow-up to a sub-agent session.",
		parameters: Type.Object({
			session_id: Type.String({ description: "The child session id." }),
			message: Type.String({ description: "Message text." }),
			mode: Type.Optional(
				Type.Union(
					[Type.Literal("prompt"), Type.Literal("steer"), Type.Literal("follow_up")],
					{ description: "Delivery mode. Defaults to prompt." },
				),
			),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
			const mode = params.mode || "prompt";
			const action = mode === "follow_up" ? "follow-up" : mode;
			const args = ["pi", params.session_id, action, params.message];
			if (mode === "prompt") args.push("--no-wait");
			const result = await ctl(pi, args, 30_000);
			return {
				content: jsonContent(result),
				details: { session_id: params.session_id, mode },
			};
		},
	});

	pi.registerTool({
		name: "agent_wait",
		label: "Wait for sub-agent",
		description:
			"Wait until a sub-agent has settled (no automatic work left). Returns whether it settled before the timeout.",
		parameters: Type.Object({
			session_id: Type.String({ description: "The child session id." }),
			timeout: Type.Optional(
				Type.Number({ description: "Timeout in seconds. Omit to wait indefinitely." }),
			),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
			const args = ["wait", params.session_id, "--settled"];
			if (params.timeout !== undefined) {
				args.push("--timeout", String(params.timeout));
			}
			const timeoutMs =
				params.timeout === undefined ? 86_400_000 : (params.timeout + 10) * 1000;
			const result = await ctl(pi, args, timeoutMs);
			return {
				content: jsonContent(result),
				details: result,
			};
		},
	});

	pi.registerTool({
		name: "agent_result",
		label: "Sub-agent result",
		description: "Read the last assistant text from a sub-agent session.",
		parameters: Type.Object({
			session_id: Type.String({ description: "The child session id." }),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
			const result = await ctl(pi, ["pi", params.session_id, "last-text"], 30_000);
			const response = (result.response || {}) as Record<string, unknown>;
			const data = (response.data || {}) as Record<string, unknown>;
			const text = typeof data.text === "string" ? data.text : "";
			return {
				content: [{ type: "text", text }],
				details: { session_id: params.session_id, text },
			};
		},
	});

	pi.registerTool({
		name: "agent_list",
		label: "List sub-agents",
		description: "List live and retained webterm sessions, including agent lineage.",
		parameters: Type.Object({}),
		async execute(_toolCallId, _params, _signal, _onUpdate, _ctx) {
			const result = await ctl(pi, ["list"], 30_000);
			const sessions = Array.isArray(result.sessions) ? result.sessions : [];
			const agents = sessions
				.filter((session) => (session as Record<string, unknown>).role === "agent")
				.map((session) => {
					const item = session as Record<string, unknown>;
					return {
						id: item.id,
						title: item.title,
						parent_id: item.parent_id,
						agent: item.agent,
						alive: item.alive,
						exit_code: item.exit_code,
						needs_input: item.needs_input,
					};
				});
			return {
				content: jsonContent(agents),
				details: { agents },
			};
		},
	});

	pi.registerTool({
		name: "agent_kill",
		label: "Kill sub-agent",
		description: "Terminate a sub-agent and its descendants.",
		parameters: Type.Object({
			session_id: Type.String({ description: "The child session id." }),
		}),
		async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
			const result = await ctl(pi, ["kill", params.session_id], 30_000);
			return {
				content: jsonContent(result),
				details: { session_id: params.session_id, ...result },
			};
		},
	});
}
