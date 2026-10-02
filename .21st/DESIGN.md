# DocVault design context

A light, neutral document SaaS workspace built with React, Vite, Tailwind v4, Geist, and existing shadcn/Radix primitives. Tokens live in `web/src/index.css`.

The desktop shell has one 232px sidebar and one main workspace. Agent contains New Run and collapsible, searchable Past Runs. A breadcrumb supplies location. New runs use a centered composer; existing runs keep the composer below the transcript. Files and Usage remain available in the same sidebar.

Use compact navigation, subtle borders, six-pixel control corners, and twelve-pixel composer corners. Avoid the separate conversation column, promotional cards, sparkle hero, gradients, and oversized active navigation tiles. Preserve accessible names, keyboard focus, responsive drawer navigation, and reduced motion.

Reference: the user's second fileAI screenshot, plus [21st sidebar patterns](https://docs.21st.dev/blog/react-sidebar-component-examples). Catalog retrieval returned HTTP 401; this implementation reuses project primitives and does not claim copied catalog code. The user chose manual browser validation.
