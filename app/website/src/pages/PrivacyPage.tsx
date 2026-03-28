import { Link } from 'react-router-dom';
import { ShieldCheck, Cloud, Eye, Database, Trash2, ArrowLeft } from 'lucide-react';
import Footer from '../components/Footer';

const InfoCard = ({
  icon: Icon,
  title,
  description,
}: {
  icon: React.ElementType;
  title: string;
  description: string;
}) => (
  <div className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-6">
    <div className="mb-4 inline-flex rounded-lg bg-[#fafafa] dark:bg-[#2a2a2a] p-3">
      <Icon className="h-6 w-6 text-[#171717] dark:text-[#ededed]" />
    </div>
    <h3 className="mb-2 text-lg font-medium text-[#171717] dark:text-[#ededed]">{title}</h3>
    <p className="text-[#666] dark:text-[#8f8f8f]">{description}</p>
  </div>
);

const Section = ({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) => (
  <div className="space-y-4">
    <h2 className="text-xl font-medium text-[#171717] dark:text-[#ededed]">{title}</h2>
    <div className="text-[#666] dark:text-[#8f8f8f] space-y-3">{children}</div>
  </div>
);

export default function PrivacyPage() {
  return (
    <div className="min-h-screen flex flex-col bg-white dark:bg-[#0a0a0a]">
      <header className="fixed top-0 left-0 right-0 z-50 bg-white/80 dark:bg-[#0a0a0a]/80 backdrop-blur-md border-b border-[#eaeaea] dark:border-[#333]">
        <div className="container mx-auto max-w-6xl px-4 md:px-6">
          <div className="flex items-center justify-between h-14">
            <Link to="/" className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-[#171717] dark:text-[#ededed]" />
              <span className="font-medium text-[#171717] dark:text-[#ededed]">Pleno Anonymize</span>
            </Link>
            <Link
              to="/"
              className="flex items-center gap-2 text-sm text-[#666] dark:text-[#8f8f8f] hover:text-[#171717] dark:hover:text-[#ededed] transition-colors"
            >
              <ArrowLeft className="h-4 w-4" />
              <span>Back</span>
            </Link>
          </div>
        </div>
      </header>

      <main className="flex-1 pt-24 pb-16">
        <div className="container mx-auto max-w-4xl px-4 md:px-6">
          <h1 className="text-4xl font-medium text-[#171717] dark:text-[#ededed] mb-8">
            Privacy Policy
          </h1>

          <div className="grid gap-6 md:grid-cols-2 mb-12">
            <InfoCard
              icon={Cloud}
              title="Cloud API"
              description="AWS上で構築したAPIサーバーで安全に処理されます"
            />
            <InfoCard
              icon={Eye}
              title="No Logging"
              description="処理されたテキストはログに保存されません"
            />
            <InfoCard
              icon={Database}
              title="No Storage"
              description="リクエストデータはメモリ上でのみ処理され、永続化されません"
            />
            <InfoCard
              icon={Trash2}
              title="Immediate Disposal"
              description="処理完了後、データは即座に破棄されます"
            />
          </div>

          <div className="space-y-8">
            <Section title="1. Overview">
              <p>
                Pleno Anonymizeは、テキストから個人情報（PII）を検出・匿名化するためのAPIです。
                本プライバシーポリシーでは、APIの利用に際してのデータの取り扱いについて説明します。
              </p>
            </Section>

            <Section title="2. Data Processing">
              <p>
                Pleno Anonymizeは合同会社Natbeeが提供するクラウドAPIサービスです。
                お客様のデータは https://anonymize.plenoai.com で安全に処理されます。
                以下のデータ処理が行われます：
              </p>
              <ul className="list-disc pl-6 space-y-2">
                <li>リクエストで送信されたテキストのPII検出</li>
                <li>検出されたPIIの匿名化処理</li>
                <li>処理結果のレスポンス返却</li>
              </ul>
            </Section>

            <Section title="3. LLM API Calls">
              <div className="rounded-lg bg-[#d3f9d8] dark:bg-[#0a3d1a] border border-[#b8f0c0] dark:border-[#166534] p-4">
                <p className="text-[#0a7227] dark:text-[#4ade80]">
                  spaCy-LLMによるNER処理時、テキストの一部がLLM APIに送信されます。
                  LLMプロバイダー側でもデータを保持しない設定を使用しており、AIモデルの学習に使用されることはありません。
                </p>
              </div>
            </Section>

            <Section title="4. Data Retention">
              <p>
                Pleno Anonymizeは、処理されたテキストを一切保存しません。
                全てのデータはメモリ上で処理され、レスポンス返却後に即座に破棄されます。
                LLMプロバイダー側でも情報を保持しないよう設定しています。
              </p>
            </Section>

            <Section title="5. No AI Training">
              <div className="rounded-lg bg-[#d3f9d8] dark:bg-[#0a3d1a] border border-[#b8f0c0] dark:border-[#166534] p-4">
                <p className="text-[#0a7227] dark:text-[#4ade80]">
                  お客様のデータがAIモデルの学習に使用されることはありません。
                </p>
              </div>
            </Section>

            <Section title="6. Security">
              <p>
                以下のセキュリティ対策を実施しています：
              </p>
              <ul className="list-disc pl-6 space-y-2">
                <li>全ての通信はHTTPSで暗号化</li>
                <li>インフラの継続的な監視</li>
              </ul>
              <p className="mt-4">
                セキュリティに関するお問い合わせは security@plenoai.com までご連絡ください。
              </p>
            </Section>

            <Section title="7. Contact">
              <p>
                プライバシーに関するお問い合わせは、GitHubのIssueまたは
                合同会社Natbeeまでお願いいたします。
              </p>
            </Section>
          </div>
        </div>
      </main>
      <Footer />
    </div>
  );
}
