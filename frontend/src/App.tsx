import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import TestResults from './pages/TestResults';
import TestSuites from './pages/TestSuites';
import LoadTests from './pages/LoadTests';
import FlowTests from './pages/FlowTests';
import HowToUse from './pages/HowToUse';
import AppShell from './components/AppShell';

function App() {
  return (
    <BrowserRouter>
      <AppShell>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/suites" element={<TestSuites />} />
          <Route path="/results" element={<TestResults />} />
          <Route path="/load-tests" element={<LoadTests />} />
          <Route path="/flows" element={<FlowTests />} />
          <Route path="/how-to-use" element={<HowToUse />} />
        </Routes>
      </AppShell>
    </BrowserRouter>
  );
}

export default App;
