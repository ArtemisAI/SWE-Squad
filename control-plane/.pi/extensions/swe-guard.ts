/**
 * Pi-agent extension: Tool Guard — Block destructive bash commands.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 */

import { createToolGuardExtension } from "../../src/extensions/tool-guard.js";

export default createToolGuardExtension({
  cwd: process.cwd(),
});
