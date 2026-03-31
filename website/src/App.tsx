import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { lazy, Suspense } from 'react';
import HomePage from './pages/HomePage';

const DocsPage = lazy(() => import('./pages/DocsPage'));
const PlaygroundPage = lazy(() => import('./pages/PlaygroundPage'));
const BenchmarkPage = lazy(() => import('./pages/BenchmarkPage'));
const PrivacyPage = lazy(() => import('./pages/PrivacyPage'));
const TermsPage = lazy(() => import('./pages/TermsPage'));

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Suspense fallback={<div className="min-h-screen bg-[#0a0a0a]" />}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/docs" element={<DocsPage />} />
          <Route path="/playground" element={<PlaygroundPage />} />
          <Route path="/benchmark" element={<BenchmarkPage />} />
          <Route path="/privacy" element={<PrivacyPage />} />
          <Route path="/terms" element={<TermsPage />} />
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
