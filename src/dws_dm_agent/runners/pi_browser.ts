/**
 * browser_run 工具扩展：驱动本机已有的 Edge / Chrome 打开页面、点击、输入、
 * 断言、截图，用来做「这个页面到底显示什么」「点一下看看有没有反应」这类验收。
 *
 * 实现走浏览器自带的 CDP 调试协议，不依赖 Selenium/Playwright，
 * 因此换一台机器拉下仓库就能用，只需要机器上装了 Chromium 系浏览器。
 *
 * 一次调用跑完整段脚本，不跨调用保留浏览器状态：没有隐藏状态、失败可复现、
 * 也不会留下占端口的孤儿进程。
 *
 * 挂载条件：runtime 里给该 agent 设置 browser: false 可关闭；默认挂载。
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/** 一次浏览器脚本的兜底超时（毫秒）。 */
const BROWSER_TOOL_TIMEOUT_MS = 180_000;

function textResult(value: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(value) }],
		details: {},
	};
}

async function callBrowserTool(request: unknown) {
	const script = process.env.DWS_BROWSER_SCRIPT;
	if (!script) {
		throw new Error("dws browser tool is not configured for this session");
	}
	const child = spawn(process.env.DWS_BROWSER_PYTHON || "python3", [script], {
		stdio: ["pipe", "pipe", "pipe"],
	});
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<unknown>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("dws browser tool timed out"));
		}, BROWSER_TOOL_TIMEOUT_MS);
		lines.on("line", (line) => {
			clearTimeout(timer);
			child.kill();
			try {
				resolve(JSON.parse(line));
			} catch (error) {
				reject(new Error(`dws browser tool returned invalid JSON: ${error}`));
			}
		});
		child.on("error", reject);
		child.stderr.on("data", () => {
			// 后端始终以 stdout JSON 表达结果，stderr 只作排障，不参与协议。
		});
		write(request);
	});
}

const STEP = Type.Object(
	{
		goto: Type.Optional(
			Type.String({ description: "URL to navigate to and wait for load" }),
		),
		click: Type.Optional(
			Type.String({ description: "CSS selector of an element to click" }),
		),
		click_text: Type.Optional(
			Type.String({
				description:
					"Click the smallest visible element whose text contains this string",
			}),
		),
		type: Type.Optional(
			Type.Object(
				{
					selector: Type.String({ description: "CSS selector of the input" }),
					text: Type.String({ description: "Text to insert" }),
					enter: Type.Optional(
						Type.Boolean({ description: "Press Enter after typing" }),
					),
				},
				{ additionalProperties: false },
			),
		),
		wait: Type.Optional(
			Type.Union([
				Type.Number({
					description: "Sleep this many milliseconds",
				}),
				Type.Object(
					{
						selector: Type.Optional(
							Type.String({ description: "Wait until this selector exists" }),
						),
						text: Type.Optional(
							Type.String({ description: "Wait until body text contains this" }),
						),
					},
					{ additionalProperties: false },
				),
			]),
		),
		text: Type.Optional(
			Type.String({
				description: "Capture the inner text of this CSS selector",
			}),
		),
		expect_text: Type.Optional(
			Type.Object(
				{
					contains: Type.String({ description: "Text that must be present" }),
					selector: Type.Optional(
						Type.String({
							description:
								"Scope the check to this selector instead of the whole page",
						}),
					),
				},
				{ additionalProperties: false },
			),
		),
		eval: Type.Optional(
			Type.String({
				description: "JavaScript expression; its value is returned",
			}),
		),
		screenshot: Type.Optional(
			Type.String({
				description: "Absolute file path to save a PNG screenshot to",
			}),
		),
		timeout_ms: Type.Optional(
			Type.Integer({
				minimum: 200,
				maximum: 60000,
				description: "Per-step timeout for waits and post-click loads",
			}),
		),
	},
	{ additionalProperties: false },
);

const browserRun = defineTool({
	name: "browser_run",
	label: "Drive a browser",
	description:
		"Open a page in a real headless browser and run a short script of steps: " +
		"navigate, click (by CSS selector or by visible text), type into inputs, " +
		"wait for a selector or for text to appear, capture text, assert that " +
		"text is present, evaluate JavaScript, and save a screenshot. Returns " +
		"per-step results plus console errors and failed network requests. Use it " +
		"to verify what a deployed page actually shows and to click through a " +
		"flow instead of guessing. An assertion that fails returns ok=false with " +
		"the text actually found, so report that failure rather than assuming " +
		"the page is fine. Each call is a fresh browser; no state carries over " +
		"between calls.",
	parameters: Type.Object(
		{
			url: Type.Optional(
				Type.String({ description: "Shorthand: navigate here before the steps" }),
			),
			steps: Type.Array(STEP, {
				description: "Ordered script; at most 40 steps",
			}),
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		const result = await callBrowserTool({
			tool: "browser_run",
			args: params,
		});
		return textResult(result);
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(browserRun);
}
