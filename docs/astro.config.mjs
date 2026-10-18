// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLinksValidator from 'starlight-links-validator';

const repo = 'https://github.com/sebastianbuechler/container-doctor';

export default defineConfig({
  // GitHub Pages serves the site from /container-doctor/.
  site: 'https://sebastianbuechler.github.io',
  base: '/container-doctor',
  integrations: [
    starlight({
      title: 'Container Doctor',
      description:
        'AI triage for the Docker containers on one host: rules detect, a model explains, you decide.',
      logo: { src: './src/assets/logo.svg' },
      favicon: '/favicon.svg',
      social: [{ icon: 'github', label: 'GitHub', href: repo }],
      editLink: { baseUrl: `${repo}/edit/main/docs/` },
      lastUpdated: true,
      customCss: ['./src/styles/custom.css'],
      // Fails the build on a link to a page or heading that does not exist.
      // The demo is a static copy of the dashboard (scripts/build-demo.mjs),
      // not a page of the site, so the validator does not know it.
      plugins: [starlightLinksValidator({ exclude: ['/container-doctor/demo/'] })],
      sidebar: [
        {
          label: 'Getting started',
          items: [
            'getting-started/introduction',
            'getting-started/quick-start',
            'getting-started/live-demo',
            'getting-started/installation',
            'getting-started/first-week',
          ],
        },
        { label: 'Concepts', items: ['concepts/how-it-works'] },
        {
          label: 'Guides',
          items: [
            { label: 'Using it', items: [
              'guides/dashboard',
              'guides/incidents',
              'guides/investigating',
              'guides/per-container',
            ] },
            { label: 'What it watches', items: [
              'guides/host',
              'guides/storage',
              'guides/network',
              'guides/changes',
              'guides/memory-limits',
              'guides/updates',
            ] },
            { label: 'Configuring it', items: [
              'guides/settings',
              'guides/models',
              'guides/classifier',
              'guides/sign-in',
            ] },
            { label: 'Running it safely', items: [
              'guides/socket-proxy',
              'guides/security',
            ] },
          ],
        },
        {
          label: 'Reference',
          items: [
            'reference/environment-variables',
            'reference/labels',
            'reference/redeploy-workflow',
            'reference/commands',
          ],
        },
        { label: 'Help', items: ['troubleshooting', 'faq'] },
        {
          label: 'Contributing',
          items: [
            'contributing/development',
            'contributing/adding-a-setting',
            'contributing/documentation',
            'contributing/releasing',
          ],
        },
        { label: 'Changelog', link: `${repo}/releases`, attrs: { target: '_blank' } },
      ],
    }),
  ],
});
