import { describe, expect, it } from 'vitest';
import { parseThinkingContent } from '../parseThinking';

describe('parseThinkingContent', () => {
  it('returns null thinkingContent when no <think> tag is present', () => {
    const input = 'Hello world, here is the answer.';
    const result = parseThinkingContent(input);
    expect(result).toEqual({
      thinkingContent: null,
      finalContent: 'Hello world, here is the answer.',
      isThinking: false,
    });
  });

  it('parses closed <think>...</think> tags correctly', () => {
    const input = '<think>I need to solve 2+2.\n2+2=4.</think>\n\nThe answer is 4.';
    const result = parseThinkingContent(input);
    expect(result).toEqual({
      thinkingContent: 'I need to solve 2+2.\n2+2=4.',
      finalContent: 'The answer is 4.',
      isThinking: false,
    });
  });

  it('handles unclosed <think>... tags during streaming', () => {
    const input = '<think>I am currently calculating the trajectory';
    const result = parseThinkingContent(input);
    expect(result).toEqual({
      thinkingContent: 'I am currently calculating the trajectory',
      finalContent: '',
      isThinking: true,
    });
  });

  it('handles multiple <think> tags by concatenating thinking content', () => {
    const input = '<think>First step reasoning</think> intermediate text <think>Second step reasoning';
    const result = parseThinkingContent(input);
    expect(result).toEqual({
      thinkingContent: 'First step reasoning\n\nSecond step reasoning',
      finalContent: 'intermediate text',
      isThinking: true,
    });
  });

  it('handles empty input string', () => {
    const result = parseThinkingContent('');
    expect(result).toEqual({
      thinkingContent: null,
      finalContent: '',
      isThinking: false,
    });
  });
});
