import { NavLink, Route, Routes } from "react-router-dom";
import BenchmarkPage from "./pages/BenchmarkPage";
import ChatPage from "./pages/ChatPage";
import TracePage from "./pages/TracePage";
import InspectorPage from "./pages/InspectorPage";

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `block rounded px-3 py-2 text-sm transition ${
    isActive ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800"
  }`;

export default function App() {
  return (
    <div className="flex h-screen">
      <aside className="flex w-56 shrink-0 flex-col gap-1 bg-slate-900 p-3">
        <div className="mb-4 px-3 text-lg font-bold text-white">Agent Runtime</div>
        <NavLink to="/" end className={linkClass}>
          Chat
        </NavLink>
        <NavLink to="/trace" className={linkClass}>
          Trace
        </NavLink>
        <NavLink to="/inspector" className={linkClass}>
          Inspector
        </NavLink>
        <NavLink to="/benchmark" className={linkClass}>
          Benchmark
        </NavLink>
      </aside>
      <main className="flex-1 overflow-hidden bg-slate-50">
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/trace" element={<TracePage />} />
          <Route path="/inspector" element={<InspectorPage />} />
          <Route path="/benchmark" element={<BenchmarkPage />} />
        </Routes>
      </main>
    </div>
  );
}
