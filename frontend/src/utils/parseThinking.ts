export interface ThinkingParseResult {
  thinkingContent: string | null;
  finalContent: string;
  isThinking: boolean;
}

/**
 * Parses <think>...</think> tags out of assistant response content.
 * Handles both closed <think>...</think> and unclosed <think>... (streaming) cases.
 */
export function parseThinkingContent(content: string): ThinkingParseResult {
  if (!content) {
    return {
      thinkingContent: null,
      finalContent: '',
      isThinking: false,
    };
  }

  const thinkRegex = /<think>([\s\S]*?)(?:<\/think>|$)/gi;
  const thinkMatches: { text: string; closed: boolean }[] = [];
  let match: RegExpExecArray | null;

  while ((match = thinkRegex.exec(content)) !== null) {
    const fullMatch = match[0];
    const thinkInner = match[1];
    const isClosed = fullMatch.toLowerCase().endsWith('</think>');
    thinkMatches.push({ text: thinkInner, closed: isClosed });
  }

  if (thinkMatches.length === 0) {
    return {
      thinkingContent: null,
      finalContent: content,
      isThinking: false,
    };
  }

  const thinkingContent = thinkMatches.map((m) => m.text).join('\n\n').trim();
  const finalContent = content
    .replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '')
    .trim();

  const isThinking = !thinkMatches[thinkMatches.length - 1].closed;

  return {
    thinkingContent: thinkingContent || null,
    finalContent,
    isThinking,
  };
}
