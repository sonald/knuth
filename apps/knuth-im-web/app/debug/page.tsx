import { DebugEventViewer } from "../../components/DebugEventViewer";
import "./debug.css";

export const metadata = {
  title: "Knuth — event viewer",
  description: "Full-fidelity RuntimeEvent inspector for debugging Knuth runs",
};

export default function DebugPage() {
  return <DebugEventViewer />;
}
