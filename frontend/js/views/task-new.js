/* Legacy new/revise routes use the same workspace composer. */
import { renderWorkspaceHome } from "./home.js";
export function renderTaskNew(ctx) {
  return renderWorkspaceHome({ ...ctx, revise: location.hash.includes("revise=1") });
}
