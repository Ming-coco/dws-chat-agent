/**
 * gitlab_project / gitlab_pipeline / gitlab_group 工具扩展（只读）：
 *   查一个 GitLab 项目的默认分支、保护分支、我的权限等级和最近流水线；
 *   查某条流水线的状态，并在失败时把失败作业的日志尾部一起带回来；
 *   查一个群组下面有哪些项目——网页上很多「仓库链接」其实指向群组。
 *
 * 用途：回答「这次合并有没有构建成功」「构建为什么挂了」「镜像出了没有」，
 * 以及判断某个分支上的改动到底走到哪一步。只读，不会创建流水线、不会合并、
 * 不会改任何东西。
 *
 * 项目一律由调用方传入，本文件不绑定任何具体仓库；凭据只经环境变量传给
 * gitlab_tools.py，不进提示词、不进命令行参数。
 *
 * 挂载条件：runtime 里给该 agent 设置 gitlab: false 可关闭；默认挂载。
 */
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/** 单次查询的兜底超时（毫秒）；GitLab 慢时不要拖住整轮。 */
const GITLAB_TOOL_TIMEOUT_MS = 45_000;

function textResult(value: unknown) {
	return {
		content: [{ type: "text" as const, text: JSON.stringify(value) }],
		details: {},
	};
}

async function callGitlabTool(request: unknown) {
	const script = process.env.DWS_GITLAB_SCRIPT;
	if (!script) {
		throw new Error("dws gitlab tool is not configured for this session");
	}
	const child = spawn(process.env.DWS_GITLAB_PYTHON || "python3", [script], {
		stdio: ["pipe", "pipe", "pipe"],
	});
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<unknown>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("dws gitlab tool timed out"));
		}, GITLAB_TOOL_TIMEOUT_MS);
		lines.on("line", (line) => {
			clearTimeout(timer);
			child.kill();
			try {
				resolve(JSON.parse(line));
			} catch (error) {
				reject(new Error(`dws gitlab tool returned invalid JSON: ${error}`));
			}
		});
		child.on("error", reject);
		child.stderr.on("data", () => {
			// 后端始终以 stdout JSON 表达结果，stderr 只作排障，不参与协议。
		});
		write(request);
	});
}

const PROJECT_PARAM = Type.String({
	description:
		"GitLab project path such as group/subgroup/project, a full web URL, or a numeric id",
});

const gitlabProject = defineTool({
	name: "gitlab_project",
	label: "Inspect GitLab project",
	description:
		"Read one GitLab project's default branch, protected branches (and the " +
		"access level each requires to merge or push), your own access level, and " +
		"the most recent pipelines. Read-only. Use it before touching a repo to " +
		"learn whether you can push at all, which branches are protected, and " +
		"whether recent builds passed. Pass the project path, not a local file " +
		"path. Nothing is created, merged or triggered.",
	parameters: Type.Object(
		{
			project: PROJECT_PARAM,
			max_pipelines: Type.Optional(
				Type.Integer({
					minimum: 1,
					maximum: 5,
					description: "How many recent pipelines to list; default 5",
				}),
			),
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		const result = await callGitlabTool({
			tool: "gitlab_project",
			args: params,
		});
		return textResult(result);
	},
});

const gitlabPipeline = defineTool({
	name: "gitlab_pipeline",
	label: "Inspect GitLab pipeline",
	description:
		"Read one GitLab pipeline's status, commit, duration and trigger source, " +
		"and when it failed, the tail of each failed job's log so you can see the " +
		"real error instead of guessing. Read-only; it never retries, cancels or " +
		"triggers a pipeline. Identify the pipeline either by iid or by ref " +
		"(branch or tag); with neither, the newest pipeline of the project is " +
		"described. Use it to answer whether a build succeeded and why it broke.",
	parameters: Type.Object(
		{
			project: PROJECT_PARAM,
			iid: Type.Optional(
				Type.Integer({
					minimum: 1,
					description: "Pipeline iid shown as #number; omit to use ref or newest",
				}),
			),
			ref: Type.Optional(
				Type.String({
					description: "Branch or tag name whose newest pipeline you want",
				}),
			),
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		const result = await callGitlabTool({
			tool: "gitlab_pipeline",
			args: params,
		});
		return textResult(result);
	},
});

const gitlabGroup = defineTool({
	name: "gitlab_group",
	label: "List projects in a GitLab group",
	description:
		"Read a GitLab group: its full path and visibility, its subgroups, and the " +
		"projects directly under it (with default branch and last activity), plus " +
		"how many projects it holds including subgroups. Read-only. Use it when a " +
		"link points at a group rather than a repo (the page lists projects instead " +
		"of showing code), or when you were given a team or customer name and need " +
		"to find the actual project paths before asking about a pipeline. Do not " +
		"guess a project path from a short name — look it up here first.",
	parameters: Type.Object(
		{
			group: Type.String({
				description:
					"GitLab group path such as group/subgroup, a full web URL, or a numeric id",
			}),
			max_projects: Type.Optional(
				Type.Integer({
					minimum: 1,
					maximum: 20,
					description: "How many direct projects to list; default 20",
				}),
			),
		},
		{ additionalProperties: false },
	),
	async execute(_toolCallId: string, params: Record<string, unknown>) {
		const result = await callGitlabTool({
			tool: "gitlab_group",
			args: params,
		});
		return textResult(result);
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(gitlabProject);
	pi.registerTool(gitlabPipeline);
	pi.registerTool(gitlabGroup);
}
