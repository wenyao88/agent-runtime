// shadcn/ui 的约定工具位（`components.json` 的 `aliases.utils` 指向这里）。
//
// 本项目用的是新一版 registry：组件直接从 **npm 包 `cn`** import（`cn` 是 clsx + tailwind-merge
// 的等价替代）。但 `components.json` 仍然承诺 `@/lib/utils` 存在 —— 之前这个文件缺失，
// 下一次 `pnpm dlx shadcn add <x>` 生成的组件会 import 一个不存在的模块（审查 m3）。
export { cn } from "cn";
