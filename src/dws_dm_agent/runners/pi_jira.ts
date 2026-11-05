/**
 * jira_issue 工具扩展（front 与 worker 都挂载，只读）：
 *   按单号或 browse 链接读取 Jira issue 的标题、状态、经办人、描述、
 *   评论、子任务、关联单据和附件清单，供模型回答「这个单怎么样了」这类
 *   问题。按次拉起 jira_tools.py，凭据只经环境变量传递，不进提示词。
 *
 * 挂载条件：配置里给该 agent 设置 jira: true（默认关闭），由 pi.py 决定
 * 是否追加 --extension。未启用时本文件的工具不会注册，模型也看不到。
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/** 单次 issue 读取的兜底超时（毫秒）；Jira 慢时不要拖住整轮。 */
const JIRA_TOOL_TIMEOUT_MS = 45_000;

function textResult(value: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(value) }],
		details: {},
	};
}

async function callJiraTool(request: unknown) {
	const script = process.env.DWS_JIRA_SCRIPT;
	if (!script) {
		throw new Error("dws jira tool is not configured for this session");
	}
	const child = spawn(process.env.DWS_JIRA_PYTHON || "python3", [script], {
		stdio: ["pipe", "pipe", "pipe"],
	});
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<unknown>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("dws jira tool timed out"));
		}, JIRA_TOOL_TIMEOUT_MS);
		lines.on("line", (line) => {
			clearTimeout(timer);
			child.kill();
			try {
				resolve(JSON.parse(line));
			} catch (error) {
				reject(new Error(`dws jira tool returned invalid JSON: ${error}`));
			}
		});
		child.on("error", reject);
		child.stderr.on("data", () => {
			// 后端始终以 stdout JSON 表达结果，stderr 只作排障，不参与协议。
		});
		write(request);
	});
}

const jiraIssue = defineTool({
	name: "jira_issue",
	label: "Read Jira issue",
	description:
		"Read one Jira issue from the company tracker: summary, status, " +
		"resolution, assignee, reporter, dates, description, recent comments, " +
		"subtasks, issue links and attachment names. Read-only; it never " +
		"creates, edits, comments on or transitions an issue. Call it whenever " +
		"the contact asks about the state, progress, scope or acceptance of a " +
		"ticket, or pastes an issue.hsjdata.com link. Pass the key " +
		"(e.g. CQNSHCYDN-1422) or the full browse URL. Status and resolution are " +
		"workflow fields and never confirm what the issue's actual state is, " +
		"whether they read 已发布/已上线 or 待发布/待测试; judge from the " +
		"description, comments, links and attachments instead, and only treat a " +
		"release as evidenced when someone says so there. Quote that evidence " +
		"with its author and time, and state plainly when the issue contains no " +
		"release record. Never guess.",
	parameters: Type.Object(
		{
			issue: Type.String({
				description:
					"Issue key such as CQNSHCYDN-1422, or a full .../browse/<KEY> URL",
			}),
			max_comments: Type.Optional(
				Type.Integer({
					minimum: 0,
					maximum: 50,
					description: "How many of the most recent comments to include; default 10",
				}),
			),
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		const result = await callJiraTool({
			tool: "jira_issue",
			args: params,
		});
		return textResult(result);
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(jiraIssue);
}
