/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emits a minimal server bundle so the production image stays small.
  output: "standalone",
  // The floating dev badge sits bottom-left, over the map UI.
  devIndicators: false,
};

export default nextConfig;
