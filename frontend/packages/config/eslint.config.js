import { waslaConfig } from "./eslint/index.js";

export default waslaConfig({ kind: "pkg-config", tsconfigRootDir: import.meta.dirname });
