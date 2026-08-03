import type {
  LanguageModelV2,
  LanguageModelV2CallOptions,
  LanguageModelV2Content,
  LanguageModelV2FinishReason,
  LanguageModelV2Message,
  LanguageModelV2StreamPart,
  LanguageModelV2Usage,
} from "@ai-sdk/provider";

/**
 * Self-contained AWS Bedrock Runtime provider for Claude, wired into the
 * existing `ai` SDK `streamText` pipeline via the LanguageModelV2 interface.
 * Because it plugs into the same `streamText().toUIMessageStreamResponse()`
 * call as every other provider in route.ts, its HTTP response shape is
 * byte-for-byte identical in protocol to the OpenAI path — this file never
 * touches the response format directly.
 *
 * Deleting this file plus its single call site in route.ts's getModel()
 * fully removes the Claude/Bedrock path.
 */

// Assumed default; verify against the target AWS account/region before
// relying on this in production. Override via BEDROCK_CLAUDE_REGION.
const DEFAULT_BEDROCK_REGION = "us-east-1";
// Assumed default model id; verify it's actually enabled in the target
// account/region before relying on this. Override via BEDROCK_CLAUDE_MODEL_ID.
const DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0";

interface BedrockContentBlock {
  text?: string;
}

interface BedrockMessage {
  role: "user" | "assistant";
  content: BedrockContentBlock[];
}

interface BedrockConverseResponse {
  output?: { message?: { content?: BedrockContentBlock[] } };
  stopReason?: string;
  usage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
}

export interface BedrockClaudeConfig {
  apiKey: string;
  modelId?: string;
  region?: string;
}

function extractText(content: Array<{ type: string; text?: string }>): string {
  return content
    .filter((part) => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text as string)
    .join("");
}

function toBedrockMessages(prompt: LanguageModelV2Message[]): {
  system: BedrockContentBlock[];
  messages: BedrockMessage[];
} {
  const system: BedrockContentBlock[] = [];
  const messages: BedrockMessage[] = [];

  for (const message of prompt) {
    if (message.role === "system") {
      system.push({ text: message.content });
      continue;
    }
    if (message.role === "user" || message.role === "assistant") {
      const text = extractText(message.content as Array<{ type: string; text?: string }>);
      if (text.length === 0) continue;
      messages.push({ role: message.role, content: [{ text }] });
    }
    // "tool" role messages aren't produced by this app's chat route today.
  }

  return { system, messages };
}

function mapFinishReason(stopReason: string | undefined): LanguageModelV2FinishReason {
  switch (stopReason) {
    case "end_turn":
    case "stop_sequence":
      return "stop";
    case "max_tokens":
      return "length";
    case "content_filtered":
      return "content-filter";
    case "tool_use":
      return "tool-calls";
    default:
      return "unknown";
  }
}

export function createBedrockClaude(config: BedrockClaudeConfig): LanguageModelV2 {
  const modelId = config.modelId || DEFAULT_BEDROCK_MODEL_ID;
  const region = config.region || DEFAULT_BEDROCK_REGION;
  const endpoint = `https://bedrock-runtime.${region}.amazonaws.com/model/${encodeURIComponent(modelId)}/converse`;

  async function callBedrock(options: LanguageModelV2CallOptions): Promise<{
    text: string;
    finishReason: LanguageModelV2FinishReason;
    usage: LanguageModelV2Usage;
  }> {
    const { system, messages } = toBedrockMessages(options.prompt);

    const inferenceConfig: Record<string, unknown> = {};
    if (options.maxOutputTokens !== undefined) inferenceConfig.maxTokens = options.maxOutputTokens;
    if (options.temperature !== undefined) inferenceConfig.temperature = options.temperature;
    if (options.topP !== undefined) inferenceConfig.topP = options.topP;
    if (options.stopSequences !== undefined) inferenceConfig.stopSequences = options.stopSequences;

    const body: Record<string, unknown> = { messages };
    if (system.length > 0) body.system = system;
    if (Object.keys(inferenceConfig).length > 0) body.inferenceConfig = inferenceConfig;

    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify(body),
      signal: options.abortSignal,
    });

    if (!response.ok) {
      const errorBody = await response.text().catch(() => "");
      throw new Error(`Bedrock Converse request failed (${response.status}): ${errorBody}`);
    }

    const data = (await response.json()) as BedrockConverseResponse;
    const text = (data.output?.message?.content ?? []).map((block) => block.text ?? "").join("");

    return {
      text,
      finishReason: mapFinishReason(data.stopReason),
      usage: {
        inputTokens: data.usage?.inputTokens,
        outputTokens: data.usage?.outputTokens,
        totalTokens: data.usage?.totalTokens,
      },
    };
  }

  return {
    specificationVersion: "v2",
    provider: "bedrock-claude",
    modelId,
    supportedUrls: {},

    async doGenerate(options) {
      const { text, finishReason, usage } = await callBedrock(options);
      const content: LanguageModelV2Content[] = text ? [{ type: "text", text }] : [];
      return { content, finishReason, usage, warnings: [] };
    },

    async doStream(options) {
      const { text, finishReason, usage } = await callBedrock(options);
      const textId = "bedrock-claude-0";

      const stream = new ReadableStream<LanguageModelV2StreamPart>({
        start(controller) {
          controller.enqueue({ type: "stream-start", warnings: [] });
          if (text) {
            controller.enqueue({ type: "text-start", id: textId });
            controller.enqueue({ type: "text-delta", id: textId, delta: text });
            controller.enqueue({ type: "text-end", id: textId });
          }
          controller.enqueue({ type: "finish", finishReason, usage });
          controller.close();
        },
      });

      return { stream };
    },
  };
}
