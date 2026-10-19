import { defineCollection } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';
import { z } from 'astro/zod';

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema({
      // The landing page's hero; see src/components/Hero.astro.
      extend: z.object({
        heroLayout: z.enum(['centered']).optional(),
      }),
    }) }),
};
