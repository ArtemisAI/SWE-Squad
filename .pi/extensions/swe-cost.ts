/**
 * Pi-agent extension: Cost Tracker — Per-session cost tracking with budget hard-stops.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 * Wraps the cost tracker extension from the control-plane.
 */

import { createCostTrackerExtension } from "../../control-plane/src/extensions/cost-tracker.js";

export default createCostTrackerExtension({
  dailyBudgetUsd: 50,
  teamId: process.env.SWE_TEAM_ID ?? "default",
});
