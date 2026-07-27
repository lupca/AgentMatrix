export interface ThinkingParseResult {
  thinkingContent: string | null;
  finalContent: string;
  isThinking: boolean;
}

/**
 * Parses <think>...</think> and <thinking>...</thinking> tags out of assistant response content.
 * Handles both closed tags and unclosed tags (streaming) cases.
 * Some models (e.g., DeepSeek) use <thinking> while others use <think>.
 */
export function parseThinkingContent(content: string): ThinkingParseResult {
  if (!content) {
    return {
      thinkingContent: null,
      finalContent: '',
      isThinking: false,
    };
  }

  // Match both <think> and <thinking> tags
  const thinkRegex = /<think(?:ing)?>([\s\S]*?)(?:<\/think(?:ing)?>|$)/gi;
  const thinkMatches: { text: string; closed: boolean }[] = [];
  let match: RegExpExecArray | null;

  while ((match = thinkRegex.exec(content)) !== null) {
    const fullMatch = match[0];
    const thinkInner = match[1];
    const isClosed = /<\/think(?:ing)?>$/i.test(fullMatch);
    thinkMatches.push({ text: thinkInner, closed: isClosed });
  }

  if (thinkMatches.length === 0) {
    return {
      thinkingContent: null,
      finalContent: content,
      isThinking: false,
    };
  }

  const thinkingContent = thinkMatches.map((m) => m.text).join('').trim();
  const finalContent = content
    .replace(/<think(?:ing)?>[\s\S]*?(?:<\/think(?:ing)?>|$)/gi, '')
    .trim();

  const isThinking = !thinkMatches[thinkMatches.length - 1].closed;

  return {
    thinkingContent: thinkingContent || null,
    finalContent,
    isThinking,
  };
}
