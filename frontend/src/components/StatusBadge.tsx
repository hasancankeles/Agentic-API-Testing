import { StatusBadge as SharedStatusBadge } from "./ui";
import type { NormalizedStatus } from "../lib/ui";

export type TestStatus = NormalizedStatus;

export default function StatusBadge({ status }: { status: TestStatus }) {
  return <SharedStatusBadge status={status} />;
}
