/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone output is for the Docker image, which copies the server bundle
  // out by hand. Vercel builds its own output and does not want it, so it is
  // switched off there rather than fighting the platform's own packaging.
  output: process.env.VERCEL ? undefined : "standalone",
  // The floating dev badge sits bottom-left, over the map UI.
  devIndicators: false,
};

export default nextConfig;
