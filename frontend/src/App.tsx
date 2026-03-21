import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import TestResults from './pages/TestResults';
import TestSuites from './pages/TestSuites';
import LoadTests from './pages/LoadTests';
import FlowTests from './pages/FlowTests';

const navItems = [
  { to: '/', label: 'Dashboard' },
  { to: '/suites', label: 'Test Suites' },
  { to: '/results', label: 'Test Results' },
  { to: '/load-tests', label: 'Load Tests' },
  { to: '/flows', label: 'Flow Tests' },
];

function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-zinc-950">
        <nav className="sticky top-0 z-50 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur-sm">
          <div className="mx-auto flex max-w-7xl items-center gap-8 px-6 py-3">
            <span className="text-lg font-semibold text-zinc-100 tracking-tight">
              Agentic API Testing
            </span>
            <div className="flex items-center gap-1">
              {navItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) =>
                    `rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                      isActive
                        ? 'bg-zinc-800 text-zinc-100'
                        : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-200'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </div>
          </div>
        </nav>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/suites" element={<TestSuites />} />
          <Route path="/results" element={<TestResults />} />
          <Route path="/load-tests" element={<LoadTests />} />
          <Route path="/flows" element={<FlowTests />} />
        </Routes>
      </div>
    </BrowserRouter>
  );
}

export default App;
