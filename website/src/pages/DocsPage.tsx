import { ApiReferenceReact } from '@scalar/api-reference-react';
import '@scalar/api-reference-react/style.css';

export default function DocsPage() {
  return (
    <ApiReferenceReact
      configuration={{
        url: `${import.meta.env.BASE_URL}openapi.json`,
        theme: 'default',
      }}
    />
  );
}
