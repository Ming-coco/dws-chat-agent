/**
 * dws 服务工具扩展（front 与 worker 都挂载）：
 *   - no_reply：零参数，表示"当前输入已处理，本次不向联系人发送消息"。
 *     terminate 结束本轮；真正的 silent 结果由服务在 tool_execution_end 捕获。
 *   - task_record / task_lookup：事项记忆读写，按次拉起 task_tools.py
 *     （复用审计 SQLite），作用域与运行信息经 DWS_TASK_CONTEXT 注入。
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/** 单次 task 工具调用的兜底超时（毫秒），避免 SQLite 挂起拖住整轮。 */
const TASK_TOOL_TIMEOUT_MS = 20_000;

function textResult(value: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(value) }],
		details: {},
	};
}

async function callTaskTool(request: unknown) {
	const script = process.env.DWS_TASK_SCRIPT;
	const context = process.env.DWS_TASK_CONTEXT;
	if (!script || !context) {
		throw new Error("dws task tools are not configured for this session");
	}
	const child = spawn(
		process.env.DWS_TASK_PYTHON || "python3",
		[script],
		{ stdio: ["pipe", "pipe", "pipe"] },
	);
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<unknown>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("dws task tool timed out"));
		}, TASK_TOOL_TIMEOUT_MS);
		lines.on("line", (line) => {
			clearTimeout(timer);
			child.kill();
			try {
				resolve(JSON.parse(line));
			} catch (error) {
				reject(new Error(`dws task tool returned invalid JSON: ${error}`));
			}
		});
		child.on("error", reject);
		child.stderr.on("data", () => {
			// 后端始终以 stdout JSON 表达结果，stderr 只作排障，不参与协议。
		});
		write(request);
	});
}

const noReply = defineTool({
	name: "no_reply",
	label: "No reply",
	description:
		"End this run without sending any message to the contact. " +
		"Call it when the current input needs no new reply: no new result, question, " +
		"risk, confirmation, or promised follow-up work remains to deliver. " +
		"It takes no arguments; the run ends silently.",
	parameters: Type.Object({}, { additionalProperties: false }),
	async execute() {
		return {
			content: [
				{
					type: "text" as const,
					text: "本次不发送消息，处理已静默结束。",
				},
			],
			details: {},
			terminate: true,
		};
	},
});

const taskRecord = defineTool({
	name: "task_record",
	label: "Record task",
	description:
		"Create or update a task in the shared task memory for this contact. " +
		"Create: omit task_id and provide goal (status defaults to open). " +
		"Update: provide task_id and its current expected_version; only submit " +
		"changed fields, omitted fields keep their current values (status never " +
		"falls back to open on update). Statuses: open (work remains), waiting " +
		"(waiting for someone; set waiting_on), completed (evidence the goal was " +
		"met), cancelled (evidence it was cancelled or superseded). Setting a " +
		"status other than waiting clears waiting_on unless you submit a new " +
		"value; submit waiting_on: \"\" to clear it explicitly. Resubmitting " +
		"identical content changes nothing. Restoring an archived task requires " +
		"restore=true together with new evidence.",
	parameters: Type.Object(
		{
			task_id: Type.Optional(
				Type.String({ description: "Existing task id; omit to create" }),
			),
			goal: Type.Optional(
				Type.String({ description: "Judgable goal of the task" }),
			),
			status: Type.Optional(
				Type.Union([
					Type.Literal("open"),
					Type.Literal("waiting"),
					Type.Literal("completed"),
					Type.Literal("cancelled"),
				]),
			),
			waiting_on: Type.Optional(
				Type.String({
					description:
						"What is being waited for, when waiting; pass \"\" to clear",
				}),
			),
			evidence_summary: Type.Optional(
				Type.String({ description: "Last observed fact; never speculation" }),
			),
			evidence_message_ids: Type.Optional(
				Type.Array(Type.String(), {
					description:
						"Message ids supporting this change; only ids from " +
						"the current run's covered messages (current input " +
						"plus supplements steered into this run); unknown " +
						"ids are rejected",
				}),
			),
			expected_version: Type.Optional(
				Type.Integer({
					description: "Current version of the task; required for updates",
				}),
			),
			restore: Type.Optional(
				Type.Boolean({ description: "Restore an archived task with new evidence" }),
			),
		},
		{ additionalProperties: false },
	),
	async execute(toolCallId: string, params: Record<string, unknown>) {
		return textResult(
			await callTaskTool({
				tool: "task_record",
				tool_call_id: toolCallId,
				args: params,
			}),
		);
	},
});

const taskLookup = defineTool({
	name: "task_lookup",
	label: "Lookup tasks",
	description:
		"Look up tasks in the current contact scope, active by default or " +
		"archived with include_archived=true. Looking up does not extend a " +
		"task's lifetime and never revives an archived task.",
	parameters: Type.Object(
		{
			status: Type.Optional(
				Type.Union([
					Type.Literal("open"),
					Type.Literal("waiting"),
					Type.Literal("completed"),
					Type.Literal("cancelled"),
				]),
			),
			include_archived: Type.Optional(Type.Boolean()),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50 })),
		},
		{ additionalProperties: false },
	),
	async execute(toolCallId: string, params: Record<string, unknown>) {
		return textResult(
			await callTaskTool({
				tool: "task_lookup",
				tool_call_id: toolCallId,
				args: params,
			}),
		);
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(noReply);
	pi.registerTool(taskRecord);
	pi.registerTool(taskLookup);
}
