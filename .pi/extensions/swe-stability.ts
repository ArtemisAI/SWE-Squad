/**
 * Pi-agent extension: Stability Gate — Block delegation when system is unstable.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 * Wraps the stability gate extension from the control-plane.
 *
 * Intercepts delegate_investigation and delegate_development tool calls,
 * checking open ticket counts before allowing execution. Without a ticket
 * store provider configured, the gate fails open (allows all calls).
 */

import { createStabilityGateExtension } from "../../control-plane/src/extensions/stability-gate.js";

export default createStabilityGateExtension({
  cacheSeconds: 60,
  maxOpenCritical: 0,
  maxOpenHigh: 3,
  onBlocked: (tool, result) => {
    console.error(
      `[StabilityGate] Blocked ${tool}: ${result.reason} ` +
        `(${result.criticalCount} critical, ${result.highCount} high)`,
    );
  },
  onWarn: (tool, result) => {
    console.warn(
      `[StabilityGate] Warning for ${tool}: ${result.reason} ` +
        `(${result.criticalCount} critical, ${result.highCount} high)`,
    );
  },
});
