import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const escalate = defineTool({
	name: "escalate",
	label: "Escalate",
	description: "Hand the conversation to the execution agent when answering requires tools, current external facts, or side effects.",
	parameters: Type.Object({
		execution: Type.Union([
			Type.Literal("read_only"),
			Type.Literal("small_change"),
			Type.Literal("plan_large_change"),
			Type.Literal("approved_plan"),
		]),
		user_message: Type.String({
			description: "Short natural Chinese progress message shown to the user.",
		}),
		reason: Type.String({
			description: "Concise internal reason for escalation.",
		}),
	}),
	async execute(_toolCallId, params) {
		return {
			content: [{ type: "text", text: "Escalated to the execution agent." }],
			details: params,
			terminate: true,
		};
	},
});

export default function (pi: ExtensionAPI) {
	pi.registerTool(escalate);
}
