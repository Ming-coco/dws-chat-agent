/**
 * e2e_run / e2e_discover / e2e_list_configs 工具扩展。
 *
 * 用途：部署之后确认页面是否正常。
 *   - e2e_run       跑一轮页面体检：登录、遍历菜单、抓错、给结论
 *   - e2e_discover  发现系统有哪些页面（接入新系统或系统改版后用）
 *   - e2e_list_configs 列出本机可用的测试配置
 *
 * 安全边界：只开放 smoke 模式（只打开页面，不填表单、不点提交）。
 * full 模式会填表单并触发查询，可能产生业务数据或触发导出，
 * 必须由人明确要求后从命令行执行，不通过这里自动触发。
 *
 * 挂载条件：runtime 里给该 agent 设置 e2e: false 可关闭；默认挂载。
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/** 页面测试要跑一两分钟，给足余量；比工具内部 900 秒上限略短。 */
const E2E_TOOL_TIMEOUT_MS = 880_000;

function textResult(value: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(value) }],
		details: {},
	};
}

async function callE2ETool(request: unknown) {
	const script = process.env.DWS_E2E_SCRIPT;
	if (!script) {
		throw new Error("dws e2e tool is not configured for this session");
	}
	const child = spawn(process.env.DWS_E2E_PYTHON || "python3", [script], {
		stdio: ["pipe", "pipe", "pipe"],
	});
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<unknown>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("dws e2e tool timed out"));
		}, E2E_TOOL_TIMEOUT_MS);
		lines.on("line", (line) => {
			clearTimeout(timer);
			child.kill();
			try {
				resolve(JSON.parse(line));
			} catch (error) {
				reject(new Error(`dws e2e tool returned invalid JSON: ${error}`));
			}
		});
		child.on("error", reject);
		child.stderr.on("data", () => {
			// 结果始终走 stdout 的 JSON，stderr 只作排障
		});
		write(request);
	});
}

const CONFIG_PARAM = Type.String({
	description:
		"配置 id，来自 e2e_list_configs 返回的 id，例如某个系统的测试环境配置名",
});

const e2eListConfigs = defineTool({
	name: "e2e_list_configs",
	label: "List E2E test configs",
	description:
		"List the page-testing configs available on this machine (id and display " +
		"name). Call this first when asked to verify a deployed system: it tells " +
		"you which systems can be tested and what id to pass to e2e_run. If it " +
		"returns nothing, this machine has no real config yet and you should say " +
		"so rather than inventing a system name.",
	parameters: Type.Object({}, { additionalProperties: false }),
	async execute(_toolCallId: string, _params: Record<string, unknown>) {
		return textResult(await callE2ETool({ tool: "e2e_list_configs", args: {} }));
	},
});

const e2eDiscover = defineTool({
	name: "e2e_discover",
	label: "Discover pages in a web system",
	description:
		"Log into a web system and work out which pages exist, by intercepting " +
		"the response that carries the menu. Use it when onboarding a system or " +
		"after the menu changed. Returns status=ok with the page list, or " +
		"needs_confirmation with the competing candidates - in that case show the " +
		"candidates to the user and ask which one is the menu instead of guessing. " +
		"Read-only: it opens pages but never submits anything.",
	parameters: Type.Object(
		{
			config: CONFIG_PARAM,
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		return textResult(
			await callE2ETool({ tool: "e2e_discover", args: params }),
		);
	},
});

const e2eRun = defineTool({
	name: "e2e_run",
	label: "Run a post-deploy page check",
	description:
		"Run a page health check against a deployed web system: log in, open " +
		"every page, capture console errors, failed requests and blank pages, " +
		"then report which page is broken and at which layer (frontend rendering, " +
		"backend interface, or unknown). Only smoke mode runs here - pages are " +
		"opened but no form is filled and nothing is submitted, so it is safe to " +
		"run unattended after a deploy. Results name only the problem pages; " +
		"report the verdict and the problems, do not list every passing page.",
	parameters: Type.Object(
		{
			config: CONFIG_PARAM,
			only: Type.Optional(
				Type.String({
					description:
						"只测名字包含该关键字的页面，例如某个菜单名；不填则测全部",
				}),
			),
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		return textResult(await callE2ETool({ tool: "e2e_run", args: params }));
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(e2eListConfigs);
	pi.registerTool(e2eDiscover);
	pi.registerTool(e2eRun);
}
