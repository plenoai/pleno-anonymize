import { Link } from 'react-router-dom';
import { ShieldCheck, ArrowLeft, CheckCircle, XCircle } from 'lucide-react';
import Footer from '../components/Footer';

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

const ListItem = ({
  type,
  children,
}: {
  type: 'allowed' | 'prohibited';
  children: React.ReactNode;
}) => (
  <li className="flex items-start gap-3">
    {type === 'allowed' ? (
      <CheckCircle className="h-5 w-5 text-[#0a7227] dark:text-[#4ade80] flex-shrink-0 mt-0.5" />
    ) : (
      <XCircle className="h-5 w-5 text-[#c00] dark:text-[#f87171] flex-shrink-0 mt-0.5" />
    )}
    <span>{children}</span>
  </li>
);

const HighlightBox = ({
  type,
  children,
}: {
  type: 'info' | 'warning' | 'success';
  children: React.ReactNode;
}) => {
  const styles = {
    info: 'bg-[#e6f4ff] dark:bg-[#0a2a3d] border-[#91caff] dark:border-[#1e40af] text-[#0050b3] dark:text-[#60a5fa]',
    warning: 'bg-[#fff8e6] dark:bg-[#3d2e0a] border-[#ffe58f] dark:border-[#92400e] text-[#915b00] dark:text-[#fbbf24]',
    success: 'bg-[#d3f9d8] dark:bg-[#0a3d1a] border-[#b8f0c0] dark:border-[#166534] text-[#0a7227] dark:text-[#4ade80]',
  };

  return (
    <div className={`rounded-lg border p-4 ${styles[type]}`}>
      {children}
    </div>
  );
};

export default function TermsPage() {
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
            Terms of Service
          </h1>

          <div className="space-y-8">
            <Section title="1. Acceptance">
              <p>
                Pleno Anonymize APIサービス（以下「本サービス」）を利用することにより、
                本利用規約に同意したものとみなされます。
              </p>
            </Section>

            <Section title="2. Service Description">
              <HighlightBox type="info">
                <p>
                  本サービスは合同会社Natbeeが https://anonymize.plenoai.com で提供するクラウドAPIサービスです。
                  テキストからPII（個人情報）を検出・匿名化する機能を提供します。
                  なお、本ソフトウェアはオープンソースとしても公開されており、セルフホストも可能です。
                </p>
              </HighlightBox>
            </Section>

            <Section title="3. Permitted Use">
              <p>以下の用途での利用が許可されています</p>
              <ul className="space-y-2 mt-4">
                <ListItem type="allowed">個人・商用目的でのAPI利用</ListItem>
                <ListItem type="allowed">API連携による社内システムへの組み込み</ListItem>
                <ListItem type="allowed">アプリケーションやサービスへの統合</ListItem>
              </ul>
            </Section>

            <Section title="4. Prohibited Use">
              <p>以下の用途での利用は禁止されています</p>
              <ul className="space-y-2 mt-4">
                <ListItem type="prohibited">違法行為への利用</ListItem>
                <ListItem type="prohibited">他者の権利を侵害する目的での利用</ListItem>
                <ListItem type="prohibited">悪意あるソフトウェアの作成</ListItem>
              </ul>
            </Section>

            <Section title="5. Service Availability">
              <p>
                本サービスは合理的な努力をもって提供されますが、100%の可用性を保証するものではありません。
                メンテナンスや予期せぬ障害により、一時的にサービスが利用できない場合があります。
              </p>
            </Section>

            <Section title="6. Disclaimer">
              <HighlightBox type="warning">
                <p>
                  本サービスは「現状のまま」提供されます。
                  合同会社Natbeeは、本サービスの利用により生じたいかなる損害についても
                  責任を負いません。PIIの検出精度は100%を保証するものではありません。
                </p>
              </HighlightBox>
            </Section>

            <Section title="7. LLM API Usage">
              <p>
                本サービスはOpenAI APIを利用しています。
                処理の一環として、テキストの一部が外部LLMプロバイダーに送信されます。
                LLMプロバイダー側でもデータを保持しない設定を使用しており、
                AIモデルの学習に使用されることはありません。
              </p>
            </Section>

            <Section title="8. Privacy">
              <p>
                データの取り扱いについては、
                <Link to="/privacy" className="text-[#0050b3] dark:text-[#60a5fa] hover:underline">
                  プライバシーポリシー
                </Link>
                をご確認ください。
              </p>
            </Section>

            <Section title="9. Changes">
              <p>
                本利用規約は予告なく変更されることがあります。
                変更後の利用規約は、GitHubリポジトリおよび本ウェブサイトに掲載された時点で
                効力を生じます。
              </p>
            </Section>

            <Section title="10. Contact">
              <p>
                本利用規約に関するお問い合わせは、GitHubのIssueまたは
                合同会社Natbeeまでお願いいたします。
              </p>
            </Section>

            <Section title="11. Governing Law">
              <p>
                本利用規約は日本法に準拠し、解釈されます。
                本サービスに関連する紛争については、東京地方裁判所を
                第一審の専属的合意管轄裁判所とします。
              </p>
            </Section>
          </div>
        </div>
      </main>
      <Footer />
    </div>
  );
}
