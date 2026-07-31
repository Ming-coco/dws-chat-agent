import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

async function callEscalate() {
	const script = process.env.DWS_ESCALATE_MCP_SCRIPT;
	const child = spawn(
		process.env.DWS_ESCALATE_PYTHON || "python3",
		script ? [script] : ["-m", "dws_dm_agent.runners.escalate_mcp"],
		{ stdio: ["pipe", "pipe", "pipe"] },
	);
	const lines = createInterface({ input: child.stdout });
	const write = (value: unknown) => child.stdin.write(`${JSON.stringify(value)}\n`);
	return await new Promise<{ content: Array<{ type: "text"; text: string }> }>((resolve, reject) => {
		const timer = setTimeout(() => {
			child.kill();
			reject(new Error("escalate MCP timed out"));
		}, 3000);
		lines.on("line", (line) => {
			const response = JSON.parse(line);
			if (response.id === 1) {
				write({ jsonrpc: "2.0", method: "notifications/initialized" });
				write({
					jsonrpc: "2.0",
					id: 2,
					method: "tools/call",
					params: { name: "escalate", arguments: {} },
				});
			} else if (response.id === 2) {
				clearTimeout(timer);
				child.kill();
				if (response.error || response.result?.isError) {
					reject(new Error(response.error?.message || "escalate MCP failed"));
				} else {
					resolve(response.result);
				}
			}
		});
		child.on("error", reject);
		write({
			jsonrpc: "2.0",
			id: 1,
			method: "initialize",
			params: {
				protocolVersion: "2025-06-18",
				capabilities: {},
				clientInfo: { name: "dws-pi-adapter", version: "0.1.0" },
			},
		});
	});
}

const escalate = defineTool({
	name: "escalate",
	label: "Escalate",
	description: "Hand the conversation to the execution agent when answering requires tools, current external facts, or side effects.",
	parameters: Type.Object({}, { additionalProperties: false }),
	async execute() {
		const result = await callEscalate();
		return {
			content: result.content,
			details: {},
			terminate: true,
		};
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(escalate);
}
