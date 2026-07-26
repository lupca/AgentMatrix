import React from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import { Check, Copy } from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import oneLight from 'react-syntax-highlighter/dist/esm/styles/prism/one-light';
import oneDark from 'react-syntax-highlighter/dist/esm/styles/prism/one-dark';

interface CodeNode {
  position?: {
    start: { line: number };
    end: { line: number };
  };
}

interface MarkdownCodeProps extends React.HTMLAttributes<HTMLElement> {
  node?: CodeNode;
}

const copyText = async (text: string) => {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
};

const MarkdownCode: React.FC<MarkdownCodeProps> = ({ className, children, node, style: _style, ...props }) => {
  const [copied, setCopied] = React.useState(false);
  const [theme, setTheme] = React.useState<'light' | 'dark'>(() => (
    document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
  ));
  const code = String(children).replace(/\n$/, '');
  const language = /language-([\w-]+)/.exec(className ?? '')?.[1];
  const isBlock = Boolean(language) || node?.position?.start.line !== node?.position?.end.line;

  React.useEffect(() => {
    const observer = new MutationObserver(() => {
      setTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);

  if (!isBlock) {
    return (
      <code className="rounded bg-black/10 px-1 py-0.5 font-mono text-[0.9em]" {...props}>
        {children}
      </code>
    );
  }

  const handleCopy = async () => {
    try {
      await copyText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied by the browser or an iframe policy.
    }
  };

  return (
    <div className="my-3 overflow-hidden rounded-lg border border-gray-700/70 bg-gray-950/70 text-left">
      <div className="flex items-center justify-between border-b border-gray-700/70 bg-gray-800/70 px-3 py-1.5 text-[10px] font-medium uppercase tracking-wide text-gray-400">
        <span>{language ?? 'code'}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1 rounded px-1.5 py-1 normal-case tracking-normal text-gray-400 transition-colors hover:bg-gray-700 hover:text-gray-100"
          aria-label="Copy code"
          title="Copy code"
        >
          {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <SyntaxHighlighter
        language={language}
        style={theme === 'dark' ? oneDark : oneLight}
        customStyle={{
          margin: 0,
          padding: '0.75rem 1rem',
          background: 'rgb(var(--color-gray-950) / 0.65)',
          fontSize: '0.75rem',
          lineHeight: 1.6,
        }}
        codeTagProps={{ className: 'font-mono' }}
        wrapLongLines
        {...props}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
};

const components: Components = {
  h1: ({ children }) => <h1 className="mb-3 mt-5 text-xl font-bold first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-4 text-lg font-bold first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-2 mt-3 text-base font-bold first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1 mt-3 font-semibold first:mt-0">{children}</h4>,
  h5: ({ children }) => <h5 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h5>,
  h6: ({ children }) => <h6 className="mb-1 mt-2 text-sm font-semibold text-gray-400 first:mt-0">{children}</h6>,
  p: ({ children }) => <p className="mb-3 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="mb-3 ml-5 list-disc space-y-1 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-3 ml-5 list-decimal space-y-1 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="pl-1">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-3 border-l-2 border-indigo-400/70 pl-3 italic text-gray-400">{children}</blockquote>
  ),
  a: ({ children, href }) => (
    <a
      href={href}
      className="text-indigo-300 underline decoration-indigo-400/50 underline-offset-2 hover:text-indigo-200"
      target="_blank"
      rel="noreferrer"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold text-inherit">{children}</strong>,
  del: ({ children }) => <del className="text-gray-400">{children}</del>,
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto rounded-lg border border-gray-700/70">
      <table className="w-full min-w-max border-collapse text-left">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border-b border-gray-700/70 bg-gray-800/60 px-3 py-2 font-semibold">{children}</th>,
  td: ({ children }) => <td className="border-b border-gray-800 px-3 py-2 last:border-b-0">{children}</td>,
  code: MarkdownCode,
  pre: ({ children }) => <>{children}</>,
  hr: () => <hr className="my-4 border-gray-700/70" />,
};

interface MessageContentProps {
  content: string;
}

export const MessageContent: React.FC<MessageContentProps> = ({ content }) => (
  <div className="markdown-content leading-relaxed">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
      components={components}
    >
      {content}
    </ReactMarkdown>
  </div>
);

export default MessageContent;
