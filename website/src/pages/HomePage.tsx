import { motion } from 'framer-motion';
import { ShieldCheck, Lock, ArrowRight, Eye, UserX, Zap, Server, Github, Star, Code, LogIn, LogOut } from 'lucide-react';
import Footer from '../components/Footer';
import { Link } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { useAuth } from '../auth/useAuth';

const GITHUB_URL = 'https://github.com/plenoai/pleno-anonymize';

const Button = ({
  variant = 'primary',
  size = 'medium',
  children,
  suffix,
  onClick,
  to,
  href,
}: {
  variant?: 'primary' | 'secondary';
  size?: 'medium' | 'large';
  children: React.ReactNode;
  suffix?: React.ReactNode;
  onClick?: () => void;
  to?: string;
  href?: string;
}) => {
  const sizeClasses = {
    medium: 'px-4 h-10 text-sm',
    large: 'px-6 h-12 text-base',
  };

  const variantClasses = {
    primary:
      'bg-[#171717] dark:bg-[#ededed] hover:bg-[#383838] dark:hover:bg-[#cccccc] text-white dark:text-[#0a0a0a]',
    secondary:
      'bg-white dark:bg-[#171717] hover:bg-[#f5f5f5] dark:hover:bg-[#2a2a2a] text-[#171717] dark:text-[#ededed] border border-[#eaeaea] dark:border-[#333]',
  };

  const className = `flex items-center justify-center gap-2 rounded-full font-medium transition-colors duration-150 ${sizeClasses[size]} ${variantClasses[variant]}`;

  if (href) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className={className}>
        <span>{children}</span>
        {suffix}
      </a>
    );
  }

  if (to) {
    return (
      <Link to={to} className={className}>
        <span>{children}</span>
        {suffix}
      </Link>
    );
  }

  return (
    <button onClick={onClick} className={className}>
      <span>{children}</span>
      {suffix}
    </button>
  );
};

const FeatureCard = ({
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

const Header = () => {
  const [starCount, setStarCount] = useState<number | null>(null);
  const { isAuthenticated, login, logout, isLoading } = useAuth();

  useEffect(() => {
    fetch('https://api.github.com/repos/plenoai/pleno-anonymize')
      .then((res) => res.json())
      .then((data) => {
        if (data.stargazers_count !== undefined) {
          setStarCount(data.stargazers_count);
        }
      })
      .catch(() => { });
  }, []);

  return (
    <header className="fixed top-0 left-0 right-0 z-50 bg-white/80 dark:bg-[#0a0a0a]/80 backdrop-blur-md border-b border-[#eaeaea] dark:border-[#333]">
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <div className="flex items-center justify-between h-14">
          <Link to="/" className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-[#171717] dark:text-[#ededed]" />
            <span className="font-medium text-[#171717] dark:text-[#ededed]">Pleno Anonymize</span>
          </Link>
          <div className="flex items-center gap-3">
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] hover:bg-[#f5f5f5] dark:hover:bg-[#2a2a2a] transition-colors"
            >
              <Github className="h-4 w-4 text-[#171717] dark:text-[#ededed]" />
              <span className="text-sm font-medium text-[#171717] dark:text-[#ededed]">GitHub</span>
              {starCount !== null && (
                <span className="flex items-center gap-1 text-sm text-[#666] dark:text-[#8f8f8f]">
                  <Star className="h-3 w-3" />
                  {starCount}
                </span>
              )}
            </a>
            {isAuthenticated ? (
              <button
                onClick={() => logout()}
                disabled={isLoading}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] hover:bg-[#f5f5f5] dark:hover:bg-[#2a2a2a] transition-colors disabled:opacity-50"
              >
                <LogOut className="h-4 w-4 text-[#171717] dark:text-[#ededed]" />
                <span className="text-sm font-medium text-[#171717] dark:text-[#ededed]">ログアウト</span>
              </button>
            ) : (
              <button
                onClick={() => login()}
                disabled={isLoading}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[#171717] dark:bg-[#ededed] hover:bg-[#383838] dark:hover:bg-[#cccccc] transition-colors disabled:opacity-50"
              >
                <LogIn className="h-4 w-4 text-white dark:text-[#0a0a0a]" />
                <span className="text-sm font-medium text-white dark:text-[#0a0a0a]">ログイン</span>
              </button>
            )}
          </div>
        </div>
      </div>
    </header>
  );
};

const HeroSection = () => (
  <section className="relative w-full overflow-hidden bg-white dark:bg-[#0a0a0a] pb-16 pt-32 md:pb-24 md:pt-40">
    <div
      className="absolute right-0 top-0 h-1/2 w-1/2"
      style={{
        background:
          'radial-gradient(circle at 70% 30%, rgba(23, 23, 23, 0.05) 0%, rgba(255, 255, 255, 0) 60%)',
      }}
    />

    <div className="container relative z-10 mx-auto max-w-6xl px-4 text-center md:px-6">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.8, ease: 'easeOut' }}
      >
        <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] px-4 py-2 text-sm">
          <ShieldCheck className="h-4 w-4 text-[#171717] dark:text-[#ededed]" />
          <span className="text-[#171717] dark:text-[#ededed]">PII Anonymization API</span>
        </div>

        <h1 className="mx-auto mb-6 max-w-4xl text-5xl font-normal tracking-tight text-[#171717] dark:text-[#ededed] md:text-6xl lg:text-7xl">
          Protect Privacy.
          <br />
          <span className="text-[#666] dark:text-[#8f8f8f]">Anonymize PII.</span>
        </h1>

        <p className="mx-auto mb-10 max-w-2xl text-lg text-[#666] dark:text-[#8f8f8f] md:text-xl">
          日本語対応の個人情報（PII）匿名化API。
          Presidio + spaCy-LLM で高精度な検出とマスキングを実現。
        </p>

        <div className="flex flex-col items-center justify-center gap-3 sm:flex-row">
          <Button variant="primary" size="large" suffix={<Server className="h-4 w-4" />} to="/docs">
            APIを使ってみる
          </Button>
          <Button variant="secondary" size="large" suffix={<ArrowRight className="h-4 w-4" />} href={GITHUB_URL}>
            GitHubで見る
          </Button>
        </div>
      </motion.div>

      <motion.div
        className="relative mt-16 md:mt-24"
        initial={{ opacity: 0, y: 40 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.8, ease: 'easeOut', delay: 0.3 }}
      >
        <div className="relative z-10 mx-auto max-w-4xl overflow-hidden rounded-2xl border border-[#eaeaea] dark:border-[#333] bg-[#1a1a1a] p-6 shadow-lg dark:shadow-none">
          <div className="flex items-center gap-2 mb-4">
            <div className="w-3 h-3 rounded-full bg-[#ff5f56]" />
            <div className="w-3 h-3 rounded-full bg-[#ffbd2e]" />
            <div className="w-3 h-3 rounded-full bg-[#27ca40]" />
          </div>
          <pre className="text-left text-sm md:text-base overflow-x-auto">
            <code className="text-[#e5e5e5]">
              {`# PII検出
curl -X POST https://anonymize.plenoai.com/api/analyze \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"text": "山田太郎さんの電話番号は090-1234-5678です。"}'

# レスポンス
[
  {"entity_type": "PERSON", "text": "山田太郎", "start": 0, "end": 4, "score": 0.85},
  {"entity_type": "PHONE_NUMBER", "text": "090-1234-5678", "start": 13, "end": 26, "score": 0.99}
]`}
            </code>
          </pre>
        </div>
      </motion.div>
    </div>
  </section>
);


const FeaturesSection = () => (
  <section className="bg-[#fafafa] dark:bg-[#111] py-24">
    <div className="container mx-auto max-w-6xl px-4 md:px-6">
      <motion.div
        className="mb-16 text-center"
        initial={{ opacity: 0, y: 20 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        transition={{ duration: 0.6 }}
      >
        <h2 className="mb-4 text-3xl font-normal text-[#171717] dark:text-[#ededed] md:text-4xl">
          主要機能
        </h2>
        <p className="mx-auto max-w-2xl text-[#666] dark:text-[#8f8f8f]">
          日本語対応の個人情報（PII）検出・匿名化サーバー
        </p>
      </motion.div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.1 }}
        >
          <FeatureCard
            icon={Eye}
            title="PII検出"
            description="人名、住所、電話番号、メールアドレスなどの個人情報を自動検出"
          />
        </motion.div>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.2 }}
        >
          <FeatureCard
            icon={UserX}
            title="匿名化（Redact）"
            description="検出されたPIIを<PERSON>などのプレースホルダーに置換"
          />
        </motion.div>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.3 }}
        >
          <FeatureCard
            icon={Zap}
            title="spaCy-LLM"
            description="高精度な日本語固有表現抽出"
          />
        </motion.div>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.4 }}
        >
          <FeatureCard
            icon={ShieldCheck}
            title="Presidio"
            description="Microsoftのpresidioによる画像対応の堅牢なPII処理基盤"
          />
        </motion.div>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.5 }}
        >
          <FeatureCard
            icon={Lock}
            title="セキュア"
            description="データはサーバー内で処理され、外部に送信されません"
          />
        </motion.div>
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, delay: 0.6 }}
        >
          <FeatureCard
            icon={Code}
            title="LLM Proxy"
            description="OpenAI/Anthropic API/Gemini APIのPII自動匿名化プロキシ"
          />
        </motion.div>
      </div>
    </div>
  </section>
);

export default function HomePage() {
  return (
    <div className="min-h-screen flex flex-col bg-white dark:bg-[#0a0a0a]">
      <Header />
      <div className="flex-1">
        <HeroSection />
        <FeaturesSection />
      </div>
      <Footer />
    </div>
  );
}
