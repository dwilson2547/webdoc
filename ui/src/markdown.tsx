import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import Editor from "@toast-ui/editor";
import codeSyntaxHighlight from "@toast-ui/editor-plugin-code-syntax-highlight";
import Prism from "prismjs";
import "prismjs/components/prism-clike";
import "prismjs/components/prism-markup-templating";
import "prismjs/components/prism-bash";
import "prismjs/components/prism-c";
import "prismjs/components/prism-cpp";
import "prismjs/components/prism-csharp";
import "prismjs/components/prism-docker";
import "prismjs/components/prism-go";
import "prismjs/components/prism-java";
import "prismjs/components/prism-json";
import "prismjs/components/prism-jsx";
import "prismjs/components/prism-markdown";
import "prismjs/components/prism-python";
import "prismjs/components/prism-rust";
import "prismjs/components/prism-sql";
import "prismjs/components/prism-tsx";
import "prismjs/components/prism-typescript";
import "prismjs/components/prism-yaml";
import "@toast-ui/editor/dist/toastui-editor.css";
import "@toast-ui/editor/dist/theme/toastui-editor-dark.css";
import "@toast-ui/editor-plugin-code-syntax-highlight/dist/toastui-editor-plugin-code-syntax-highlight.css";

// Toast UI Editor, as in dan-wiki (ai/dan-wiki/frontend/src/components/MarkdownEditor.jsx), but
// driven directly rather than through @toast-ui/react-editor, whose wrapper predates React 19.
// Markdown mode only: the WYSIWYG tab is hidden so the stored text is exactly what you typed.

const dark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches;
const plugins = [[codeSyntaxHighlight, { highlighter: Prism }]] as any;

export type EditorHandle = { getMarkdown: () => string; focus: () => void };

export const MarkdownEditor = forwardRef<EditorHandle, {
  initial: string; onChange?: (md: string) => void; height?: string;
}>(function MarkdownEditor({ initial, onChange, height = "calc(100vh - 230px)" }, ref) {
  const host = useRef<HTMLDivElement>(null);
  const ed = useRef<Editor | null>(null);
  const change = useRef(onChange);
  change.current = onChange;

  useEffect(() => {
    const e = new Editor({
      el: host.current!,
      initialValue: initial,
      initialEditType: "markdown",
      previewStyle: "vertical",
      hideModeSwitch: true,
      height,
      usageStatistics: false,
      theme: dark() ? "dark" : "light",
      plugins,
      toolbarItems: [
        ["heading", "bold", "italic", "strike"],
        ["hr", "quote"],
        ["ul", "ol", "task", "indent", "outdent"],
        ["table", "link"],
        ["code", "codeblock"],
        ["scrollSync"],
      ],
      events: { change: () => change.current?.(e.getMarkdown()) },
    });
    ed.current = e;
    return () => { e.destroy(); ed.current = null; };
    // The editor owns its text after mount; `initial` only seeds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useImperativeHandle(ref, () => ({
    getMarkdown: () => ed.current?.getMarkdown() ?? "",
    focus: () => ed.current?.focus(),
  }), []);

  return <div ref={host} className="md-editor" />;
});

export function MarkdownView({ markdown }: { markdown: string }) {
  const host = useRef<HTMLDivElement>(null);
  const viewer = useRef<ReturnType<typeof Editor.factory> | null>(null);
  useEffect(() => {
    viewer.current = Editor.factory({
      el: host.current!, viewer: true, initialValue: markdown, usageStatistics: false,
      theme: dark() ? "dark" : "light", plugins,
    });
    return () => { viewer.current?.destroy(); viewer.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { viewer.current?.setMarkdown(markdown); }, [markdown]);
  return <div ref={host} className="md-view" />;
}
