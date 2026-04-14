/**
 * Pi-agent extension: Cost Tracker — Per-session cost tracking with budget hard-stops.
 *
 * Discovered automatically by pi-agent from .pi/extensions/.
 */

import { createCostTrackerExtension } from "../../src/extensions/cost-tracker.js";

export default createCostTrackerExtension({
  dailyBudgetUsd: 50,
  teamId: process.env.SWE_TEAM_ID ?? "default",
});
