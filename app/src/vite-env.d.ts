// Typed Vite env. Mirrors the Loadopoly-OCR variable names so StockOpoly can
// point at the same Supabase project; all optional so the app builds with none.
interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_ANON_KEY?: string;
  readonly VITE_ENGINE_URL?: string;
  readonly VITE_BASE?: string;
  readonly [key: string]: string | undefined;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
