import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { promisify } from "node:util";
import { isAbsolute, relative, resolve, sep } from "node:path";
import type { Usage } from "@earendil-works/pi-ai";
import type { ExtensionAPI, SessionEntry } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

function formatTokens(count: number): string {
	if (count < 1_000) return String(count);
	if (count < 10_000) return `${(count / 1_000).toFixed(1)}k`;
	if (count < 1_000_000) return `${Math.round(count / 1_000)}k`;
	if (count < 10_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
	return `${Math.round(count / 1_000_000)}M`;
}

const thinkingLabels = {
	off: "off",
	minimal: "min",
	low: "L",
	medium: "M",
	high: "H",
	xhigh: "XH",
	max: "MAX",
} as const;

function singleLine(text: string): string {
	return text.replace(/[\r\n\t]/g, " ").replace(/ +/g, " ").trim();
}

function formatPath(cwd: string): string {
	const path = relative(resolve(homedir()), resolve(cwd));
	if (path === "") return "~";
	return path === ".." || path.startsWith(`..${sep}`) || isAbsolute(path) ? cwd : `~${sep}${path}`;
}

function entryUsage(entry: SessionEntry): Usage | undefined {
	if (entry.type === "usage" || entry.type === "compaction" || entry.type === "branch_summary") {
		return entry.usage;
	}
	if (entry.type === "message" && (entry.message.role === "assistant" || entry.message.role === "toolResult")) {
		return entry.message.usage;
	}
	return undefined;
}

const execFileAsync = promisify(execFile);

async function readGitStatus(cwd: string, signal: AbortSignal) {
	const { stdout } = await execFileAsync("git", [
		"status", "--porcelain=v1", "--branch", "-z", "--untracked-files=normal",
	], {
		cwd, signal, timeout: 5_000, maxBuffer: 4 * 1024 * 1024,
		env: { ...process.env, GIT_OPTIONAL_LOCKS: "0" },
	});
	const headerEnd = stdout.indexOf("\0");
	const header = stdout.slice(0, headerEnd);
	let branch = header.replace(/^## (?:No commits yet on |Initial commit on )?/, "").split("...")[0];
	if (branch === "HEAD (no branch)") branch = "detached";
	return { branch, dirty: stdout.length > headerEnd + 1 };
}

export default function (pi: ExtensionAPI) {
	let refreshGit: (() => void) | undefined;
	let disposeFooter: (() => void) | undefined;
	pi.on("tool_execution_end", () => { refreshGit?.(); });
	pi.on("session_shutdown", () => { disposeFooter?.(); });
	pi.on("session_start", (_event, ctx) => {
		if (ctx.mode !== "tui") return;

		ctx.ui.setFooter((tui, theme, footerData) => {
			disposeFooter?.();
			let disposed = false;
			let pending = false;
			let queued = false;
			let gitStatus = { cwd: ctx.cwd, branch: footerData.getGitBranch(), dirty: false };
			let controller: AbortController | undefined;

			async function updateGit() {
				if (disposed) return;
				if (pending) { queued = true; return; }
				pending = true;
				const cwd = ctx.cwd;
				controller = new AbortController();
				let next = { cwd, branch: null as string | null, dirty: false };
				try {
					next = { cwd, ...await readGitStatus(cwd, controller.signal) };
				} catch {
					// Outside a repository or unavailable Git: show the path alone.
				} finally {
					pending = false;
					controller = undefined;
				}
				if (disposed) return;
				if (next.cwd !== gitStatus.cwd || next.branch !== gitStatus.branch || next.dirty !== gitStatus.dirty) {
					gitStatus = next;
					tui.requestRender();
				}
				if (queued) { queued = false; void updateGit(); }
			}

			const requestGitRefresh = () => { void updateGit(); };
			refreshGit = requestGitRefresh;
			const unsubscribe = footerData.onBranchChange(requestGitRefresh);
			// Poll for external edits as well as refreshing immediately after Pi tools.
			const timer = setInterval(requestGitRefresh, 2_000);
			timer.unref();
			requestGitRefresh();
			const dispose = () => {
				if (disposed) return;
				disposed = true;
				clearInterval(timer);
				controller?.abort();
				unsubscribe();
				if (refreshGit === requestGitRefresh) refreshGit = undefined;
				if (disposeFooter === dispose) disposeFooter = undefined;
			};
			disposeFooter = dispose;
			let cachedKey: string | undefined;
			let tokenParts: string[] = [];

			function updateTokens() {
				const manager = ctx.sessionManager;
				const key = `${manager.getSessionId()}:${manager.getLeafId()}`;
				if (key === cachedKey) return;
				const totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
				let cacheHitRate: number | undefined;
				// Include the entire session, compaction, cache warming, and nested-tool usage.
				for (const entry of manager.getEntries()) {
					const usage = entryUsage(entry);
					if (usage) {
						totals.input += usage.input;
						totals.output += usage.output;
						totals.cacheRead += usage.cacheRead;
						totals.cacheWrite += usage.cacheWrite;
					}
					if (entry.type === "message" && entry.message.role === "assistant") {
						const u = entry.message.usage;
						const promptTokens = u.input + u.cacheRead + u.cacheWrite;
						cacheHitRate = promptTokens > 0 ? (u.cacheRead / promptTokens) * 100 : undefined;
					}
				}
				tokenParts = [`↑${formatTokens(totals.input)} ↓${formatTokens(totals.output)}`];
				if ((totals.cacheRead || totals.cacheWrite) && cacheHitRate !== undefined) {
					tokenParts.push(`CH${cacheHitRate.toFixed(1)}%`);
				}
				cachedKey = key;
			}

			return {
				dispose,
				invalidate() { cachedKey = undefined; },
				render(width: number): string[] {
					if (width <= 0) return [""];
					updateTokens();
					const model = ctx.model;
					const usage = ctx.getContextUsage();
					const percent = usage?.percent;
					const contextWindow = usage?.contextWindow ?? model?.contextWindow ?? 0;
					const autoCompactDisabled = pi.getSettings().compaction?.enabled === false;
					const context = `${percent === null ? "?" : (percent ?? 0).toFixed(1) + "%"}/${formatTokens(contextWindow)}`;
					const contextColor = (percent ?? 0) > 90 ? "error" : (percent ?? 0) > 70 ? "warning" : "dim";
					const separator = theme.fg("dim", " | ");
					const statsParts = [...tokenParts.map(part => theme.fg("dim", part)), theme.fg(contextColor, context)];
					if (autoCompactDisabled) statsParts.push(theme.fg("warning", "auto-off"));
					const stats = statsParts.join(" ");

					let modelText = singleLine(model?.id ?? "no-model");
					if (model?.reasoning) {
						const level = ctx.thinkingLevel ?? pi.getThinkingLevel();
						modelText += `·${thinkingLabels[level]}`;
					}
					// Keep usage visible when the terminal narrows; shorten model and path first.
					const modelBudget = width - visibleWidth(stats) - visibleWidth(separator);
					if (modelBudget <= 0) return [truncateToWidth(stats, width, "…")];
					const right = `${theme.fg("dim", truncateToWidth(modelText, modelBudget, "…"))}${separator}${stats}`;

					const branch = gitStatus.cwd === ctx.cwd ? gitStatus.branch : null;
					const name = ctx.sessionManager.getSessionName();
					let leftText = formatPath(ctx.cwd);
					if (branch) leftText += ` ${branch}${gitStatus.dirty ? "*" : ""}`;
					if (name) leftText += ` • ${name}`;
					const statuses = [...footerData.getExtensionStatuses().entries()]
						.sort(([a], [b]) => a.localeCompare(b))
						.map(([, text]) => singleLine(text)).filter(Boolean);
					if (statuses.length) leftText += ` • ${statuses.join(" ")}`;
					const leftBudget = width - visibleWidth(right) - 2;
					if (leftBudget <= 0) return [right];
					const left = theme.fg("dim", truncateToWidth(singleLine(leftText), leftBudget, "…"));
					return [left + " ".repeat(width - visibleWidth(left) - visibleWidth(right)) + right];
				},
			};
		});
	});
}
