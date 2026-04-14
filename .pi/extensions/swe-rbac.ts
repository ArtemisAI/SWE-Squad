/**
 * Pi-agent extension: RBAC — Role-based tool access control.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 * Wraps the RBAC extension from the control-plane.
 */

import { createRBACExtension } from "../../control-plane/src/extensions/rbac.js";

export default createRBACExtension({
  role: "full",
  cwd: process.cwd(),
});
