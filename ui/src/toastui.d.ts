// @toast-ui/editor ships types (types/index.d.ts) but its package.json "exports" hides them from
// bundler resolution, so the parts used here are declared directly.
declare module "@toast-ui/editor" {
  type Options = Record<string, unknown> & { el: HTMLElement };
  interface Instance {
    getMarkdown(): string;
    setMarkdown(md: string): void;
    focus(): void;
    destroy(): void;
  }
  export default class Editor implements Instance {
    constructor(options: Options);
    static factory(options: Options & { viewer?: boolean }): Instance;
    getMarkdown(): string;
    setMarkdown(md: string): void;
    insertText(text: string): void;
    moveCursorToEnd(focus?: boolean): void;
    focus(): void;
    destroy(): void;
  }
}
declare module "@toast-ui/editor-plugin-code-syntax-highlight";
