/**
 * Pi-agent extension: Tool Guard — Block destructive bash commands.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 * Wraps the tool guard extension from the control-plane.
 */

import { createToolGuardExtension } from "../../control-plane/src/extensions/tool-guard.js";

export default createToolGuardExtension({
  cwd: process.cwd(),
});
