// content.js - Robust Version 4.0 (Interactive Video + Deep Images)
(() => {
    console.log("🚀 AquaVerve Collector v4.0 Starting...");

    // --- Selectors Configuration ---
    const SELECTORS = {
        // IMPROVED: More specific and robust title selectors (ordered by priority)
        title: [
            ".right_info h1",           // Dajian specific: product title in right info panel
            ".product-detail h1",       // Product detail page
            ".product-info h1",         // Alternative layout
            "#product-name",            // By ID
            ".product-title",           // By class
            "h1.title",                 // H1 with title class
            "h1[data-product-title]",   // H1 with data attribute
            ".pro_info h1",             // Product info section
            "h1"                        // Last resort: any H1
        ],
        sku: ["#itemCode", ".sku", ".product-code", ".item-code", ".pro_id", "#pro_id", ".product-sku"],
        price: [".price-valid", ".product-price", ".price", ".p-price span", ".current-price"],
        activeVariant: [".options-item-active", ".spec-item.selected", ".attr-item.active", ".pro_spec li.active", ".variant-active"],
        images: [".swiper-slide img", "#pro_big_img img", ".gallery-thumbs img", ".product-image img"],
        // Video trigger buttons (thumbnails with play icon)
        videoTrigger: [".el-icon-video-play", ".gc-video", ".gc-video-a", ".video-thumbnail", ".image-item .icon-play"]
    };

    // IMPROVED: More robust text extraction with validation
    function getElementText(selectorList) {
        for (const sel of selectorList) {
            const el = document.querySelector(sel);
            if (el && el.innerText && el.innerText.trim()) {
                const text = el.innerText.trim();
                // Filter out obviously wrong values (variant names, short labels, etc.)
                if (text.length > 5 && !isLikelyVariantName(text)) {
                    return text;
                }
            }
        }
        return null;
    }
    
    // NEW: Helper to detect if text is likely a variant name, not a product title
    function isLikelyVariantName(text) {
        const variantPatterns = [
            /^(extra|standard|default|basic|premium|pro|plus)$/i,
            /^(small|medium|large|xl|xxl|xs|s|m|l)$/i,
            /^(red|blue|green|black|white|gray|grey|brown|beige|walnut|oak)$/i,
            /^(left|right|single|double|twin|queen|king|full)$/i,
            /^\d+\s*(pcs?|pieces?|pack|set)$/i,
            /^\d+["']?\s*x\s*\d+["']?$/i,  // Dimension like "24x36"
        ];
        return variantPatterns.some(pattern => pattern.test(text));
    }

    function escapeHtml(text) {
        return String(text || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function collectStructuredImageUrls() {
        const urls = [];

        document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"], meta[itemprop="image"]').forEach(meta => {
            if (meta.content) {
                urls.push(meta.content.trim());
            }
        });

        const visit = (node) => {
            if (!node) return;
            if (Array.isArray(node)) {
                node.forEach(visit);
                return;
            }
            if (typeof node === "string") {
                if (/^https?:\/\//i.test(node) && /\.(?:jpg|jpeg|png|webp)(?:$|\?)/i.test(node)) {
                    urls.push(node);
                }
                return;
            }
            if (typeof node === "object") {
                ["image", "thumbnailUrl", "contentUrl", "primaryImageOfPage"].forEach(key => {
                    if (node[key]) visit(node[key]);
                });
            }
        };

        document.querySelectorAll('script[type="application/ld+json"]').forEach(script => {
            try {
                visit(JSON.parse(script.textContent || ""));
            } catch (error) {
                console.warn("📸 JSON-LD parse failed", error);
            }
        });

        return urls;
    }

    // --- 1. Interactive Video Extraction ---
    // Simulates a user click to load the video modal, extracts URL, then closes it.
    async function extractVideoInteractive() {
        const videoUrls = new Set();

        // Helper to add URL with deduplication (ignore query params)
        const addVideoUrl = (url) => {
            if (!url) return;
            const baseUrl = url.split('?')[0]; // Remove query params for dedup
            videoUrls.add(baseUrl);
        };

        // 1. Try finding an existing video first (Nuclear Scan)
        const passiveVideos = scanForVideosStrict();
        passiveVideos.forEach(v => addVideoUrl(v));

        // 2. Interactive Discovery - Click the video play icon
        // Try multiple selectors (different pages use different classes)
        const playIcon = document.querySelector('.gc-play-one') ||
            document.querySelector('.el-icon-video-play') ||
            document.querySelector('[class*="play"]');

        if (playIcon && videoUrls.size === 0) { // Only try if passive failed
            console.log("🎥 Found Play Icon, attempting interactive extraction...", playIcon.className);

            try {
                // Click the icon (or its parent carousel item)
                const clickTarget = playIcon.closest('.el-carousel__item') || playIcon.parentElement || playIcon;
                clickTarget.click();
                console.log("🎥 Clicked video trigger, waiting for dialog...");

                // Wait for the el-dialog to appear and video to load
                await new Promise(r => setTimeout(r, 2500));

                // Find the video dialog (the one with "视频" title)
                const dialogs = Array.from(document.querySelectorAll('.el-dialog'));
                const videoDialog = dialogs.find(d =>
                    d.innerText.includes('视频') &&
                    window.getComputedStyle(d).display !== 'none'
                ) || document.querySelector('.el-dialog'); // Fallback to any visible dialog

                if (videoDialog) {
                    console.log("🎥 Video dialog found!");

                    // Try multiple ways to get the video URL
                    const videoEl = videoDialog.querySelector('video');
                    const sourceEl = videoDialog.querySelector('source');

                    let videoUrl = null;
                    if (videoEl && videoEl.src) {
                        videoUrl = videoEl.src;
                    } else if (sourceEl && sourceEl.src) {
                        videoUrl = sourceEl.src;
                    }

                    if (videoUrl) {
                        addVideoUrl(videoUrl); // Will auto-deduplicate
                        console.log(`🎥 Extracted video: ${videoUrl.split('?')[0]}`);
                    } else {
                        console.warn("🎥 Dialog found but no video/source element");
                    }
                }

                // Also do a fresh nuclear scan in case video is now in DOM
                const freshVideos = scanForVideosStrict();
                freshVideos.forEach(v => addVideoUrl(v)); // Will auto-deduplicate

                // Close modal
                const closeBtn = document.querySelector(".el-dialog__headerbtn .el-dialog__close") ||
                    document.querySelector(".el-dialog__headerbtn");
                if (closeBtn) {
                    closeBtn.click();
                    console.log("🎥 Closed video dialog");
                }

            } catch (e) {
                console.warn("Interactive video extraction failed", e);
            }
        }

        return Array.from(videoUrls);
    }

    // Nuclear Scan (Helper for interactive mode)
    function scanForVideosStrict() {
        const videoUrls = new Set();

        const addUrl = (u) => {
            if (!u) return;
            if (u.startsWith("//")) u = "https:" + u;
            if (u.startsWith("/")) u = window.location.origin + u;
            if (u.startsWith("http")) videoUrls.add(u);
        };

        // DOM
        document.querySelectorAll("video").forEach(v => {
            addUrl(v.src);
            v.querySelectorAll("source").forEach(s => addUrl(s.src));
        });
        document.querySelectorAll("iframe").forEach(f => {
            if (f.src && (f.src.includes("youtube") || f.src.includes("vimeo") || f.src.includes("aliyun"))) {
                addUrl(f.src);
            }
        });

        // Inline page state / SSR fallbacks
        const scriptText = Array.from(document.scripts || [])
            .map(s => s.textContent || "")
            .join("\n");
        const pageText = `${document.documentElement ? document.documentElement.innerHTML : ""}\n${scriptText}`;

        const productVideoMatch = pageText.match(/productVideoUrl["']?\s*[:=]\s*["']([^"']+\.mp4[^"']*)["']/i);
        if (productVideoMatch) {
            addUrl(productVideoMatch[1]);
        }

        const videoArrayPattern = /videoUrls["']?\s*[:=]\s*\[(.*?)\]/gis;
        let arrayMatch;
        while ((arrayMatch = videoArrayPattern.exec(pageText)) !== null) {
            const mp4Matches = arrayMatch[1].match(/https?:\/\/[^"'\\\s<>]+\.mp4[^"'\\\s<>]*/gi);
            if (mp4Matches) {
                mp4Matches.forEach(addUrl);
            }
        }

        // Regex (Deep scan for mp4 strings in HTML)
        const html = pageText || document.body.innerHTML;
        const absMatches = html.match(/https?:\/\/[^"'\s<>]+\.mp4/gi);
        if (absMatches) absMatches.forEach(addUrl);

        // Relative Regex
        const relMatches = html.match(/['"](\/[^"'\s<>]+\.mp4)['"]/gi);
        if (relMatches) {
            relMatches.forEach(m => addUrl(m.replace(/['"]/g, "")));
        }

        return Array.from(videoUrls);
    }


    // --- 2. Image Scanning (Enhanced v5.0) ---
    // FIXED: Only collect images from the ACTIVE product, not other SKU variants
    function scanForImages() {
        const priorityImages = [];  // Main carousel images (use array to preserve order)
        const secondaryImages = new Set();
        const seenUrls = new Set();

        // Get current product's seller ID from URL if available
        // GigaCloud images have wkseller/ID pattern - we want consistent seller
        let primarySellerId = null;

        const addUrl = (u, isPriority = false) => {
            if (!u) return null;
            u = u.trim();
            if (u.startsWith("//")) u = "https:" + u;
            if (u.startsWith("/")) u = window.location.origin + u;

            if (u.startsWith("http")) {
                // Filter out common junk
                if (u.includes("logo") || u.includes("icon") ||
                    u.includes("empty") || u.includes("placeholder") ||
                    u.includes("product_base") || u.includes("default") ||
                    u.includes("avatar") || u.includes("badge") ||
                    u.includes("flag") || u.includes("banner")) {
                    return null;
                }

                // Remove resize parameters to get full-size image
                // GigaCloud uses: x-oss-process=image%2Fresize%2Cw_368%2Ch_368
                let cleanUrl = u;
                if (u.includes("x-oss-process")) {
                    // Keep URL up to x-oss-process, then modify to get larger image
                    cleanUrl = u.replace(/x-oss-process=image%2Fresize[^&]*/g, 
                        'x-oss-process=image%2Fresize%2Cw_800%2Ch_800%2Cm_pad');
                }

                // Extract seller ID for consistency check
                const sellerMatch = cleanUrl.match(/wkseller\/(\d+)\//);
                if (sellerMatch) {
                    const sellerId = sellerMatch[1];
                    // Set primary seller from first high-quality image found
                    if (!primarySellerId && isPriority) {
                        primarySellerId = sellerId;
                        console.log(`📸 Primary seller ID set: ${sellerId}`);
                    }
                    // Skip images from different sellers (SKU variant images)
                    if (primarySellerId && sellerId !== primarySellerId) {
                        console.log(`📸 Skipping image from different seller: ${sellerId} (primary: ${primarySellerId})`);
                        return null;
                    }
                }

                // Deduplicate by base URL (ignore query params for comparison)
                const baseUrl = cleanUrl.split('?')[0];
                if (seenUrls.has(baseUrl)) {
                    return null;
                }
                seenUrls.add(baseUrl);

                if (isPriority) {
                    priorityImages.push(cleanUrl);
                } else {
                    secondaryImages.add(cleanUrl);
                }
                return cleanUrl;
            }
            return null;
        };

        // A. HIGHEST PRIORITY: Main product carousel (el-carousel is GigaCloud's main gallery)
        // These are the true product images, click through slides first
        const carouselContainer = document.querySelector('.el-carousel, .product-gallery, #pro_big_img');
        if (carouselContainer) {
            console.log("📸 Found main carousel container");
            // Get images from carousel items
            carouselContainer.querySelectorAll('.el-carousel__item img, .swiper-slide img, img').forEach(img => {
                const src = img.getAttribute('data-original') ||
                    img.getAttribute('data-src') ||
                    img.src;
                if (src && (img.naturalWidth > 100 || img.width > 100 || src.includes('b2bfiles'))) {
                    addUrl(src, true);
                }
            });
        }

        // B. Gallery thumbnails (below main image)
        document.querySelectorAll('.gallery-thumbs img, .product-thumbs img, .thumb-list img').forEach(img => {
            const src = img.getAttribute('data-original') || img.getAttribute('data-src') || img.src;
            addUrl(src, true);
        });

        // C. Product detail images (in description area) - ONLY from same seller
        const detailContainer = document.querySelector('#pane-description, .product-detail-content, .product-description');
        if (detailContainer) {
            detailContainer.querySelectorAll('img').forEach(img => {
                const src = img.getAttribute('data-original') || img.src;
                // Only add if from same seller and reasonable size
                if (src && src.includes('b2bfiles') && 
                    (img.naturalWidth > 300 || img.width > 300 || src.includes('big'))) {
                    addUrl(src, false);  // Lower priority
                }
            });
        }

        // D. DO NOT do a general deep scan - this picks up SKU variant images!
        // Only scan specific known containers for additional images

        // D. Structured data fallback (useful on pages where gallery DOM is lazy-loaded)
        collectStructuredImageUrls().forEach(url => addUrl(url, priorityImages.length === 0));

        // Combine results: priority first, then secondary
        const allImages = [...priorityImages];
        secondaryImages.forEach(img => {
            if (!seenUrls.has(img.split('?')[0]) || !priorityImages.some(p => p.split('?')[0] === img.split('?')[0])) {
                allImages.push(img);
            }
        });

        console.log(`📸 Collected ${priorityImages.length} priority + ${secondaryImages.size} secondary images`);
        
        // Keep up to 24 images; flag low-image pages during validation instead of truncating aggressively.
        return allImages.slice(0, 24);
    }

    // --- 3. Tab & Product Info Extraction ---
    function extractProductInfo() {
        const info = { features: [], specs: {} };

        // IMPROVED: Also scan the Specification section directly on the page
        // GigaCloud/Dajian shows specs in a structured format like:
        // "Assembled Length (in.): 76.00"
        
        // 1. First try to extract from the visible page content (Product Dimensions section)
        const bodyText = document.body.innerText;
        
        // Enhanced spec patterns for GigaCloud/Dajian format
        const specPatterns = [
            // Assembled dimensions: "Assembled Length (in.): 76.00"
            { pattern: /Assembled Length\s*\(?in\.?\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Length (in.)" },
            { pattern: /Assembled Width\s*\(?in\.?\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Width (in.)" },
            { pattern: /Assembled Height\s*\(?in\.?\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Height (in.)" },
            { pattern: /Product Weight\s*\(?lbs?\.?\)?[:\s]+(\d+\.?\d*)/i, key: "Product Weight (lbs.)" },
            // Also match Chinese formats
            { pattern: /产品长度[:\s]+(\d+\.?\d*)/i, key: "Assembled Length (in.)" },
            { pattern: /产品宽度[:\s]+(\d+\.?\d*)/i, key: "Assembled Width (in.)" },
            { pattern: /产品高度[:\s]+(\d+\.?\d*)/i, key: "Assembled Height (in.)" },
            { pattern: /产品重量[:\s]+(\d+\.?\d*)/i, key: "Product Weight (lbs.)" },
            { pattern: /组装长度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Length (in.)" },
            { pattern: /组装宽度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Width (in.)" },
            { pattern: /组装高度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Height (in.)" },
            { pattern: /产品重量\s*\(?.{0,4}磅.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Product Weight (lbs.)" },
            // General patterns
            { pattern: /Main Material[:\s]+([A-Za-z+\s]+?)(?:\n|$)/i, key: "Main Material" },
            { pattern: /主材质[:\s]+([^\n]+)/i, key: "Main Material" },
            { pattern: /Main Color[:\s]+([A-Za-z\s]+?)(?:\n|$)/i, key: "Main Color" },
            { pattern: /主颜色[:\s]+([^\n]+)/i, key: "Main Color" },
        ];
        
        specPatterns.forEach(({ pattern, key }) => {
            const match = bodyText.match(pattern);
            if (match && match[1]) {
                info.specs[key] = match[1].trim();
                console.log(`📐 Extracted ${key}: ${match[1].trim()}`);
            }
        });

        // Extract structured "产品尺寸 / 包装尺寸" sections from the visible page text.
        const productSizeBlock = bodyText.match(/产品尺寸([\s\S]{0,240})包装尺寸/i);
        if (productSizeBlock && productSizeBlock[1]) {
            const section = productSizeBlock[1];
            const productSizePatterns = [
                { pattern: /组装长度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Length (in.)" },
                { pattern: /组装宽度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Width (in.)" },
                { pattern: /组装高度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Assembled Height (in.)" },
                { pattern: /产品重量\s*\(?.{0,4}磅.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Product Weight (lbs.)" },
            ];
            productSizePatterns.forEach(({ pattern, key }) => {
                const match = section.match(pattern);
                if (match && match[1]) {
                    info.specs[key] = match[1].trim();
                }
            });
        }

        const packageSizeBlock = bodyText.match(/包装尺寸([\s\S]{0,240})(产品特点|图文描述|Product Features|$)/i);
        if (packageSizeBlock && packageSizeBlock[1]) {
            const section = packageSizeBlock[1];
            const packageSizePatterns = [
                { pattern: /长度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Package Length (in.)" },
                { pattern: /宽度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Package Width (in.)" },
                { pattern: /高度\s*\(?.{0,4}英寸.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Package Height (in.)" },
                { pattern: /重量\s*\(?.{0,4}磅.{0,2}\)?[:\s]+(\d+\.?\d*)/i, key: "Package Weight (lbs.)" },
            ];
            packageSizePatterns.forEach(({ pattern, key }) => {
                const match = section.match(pattern);
                if (match && match[1]) {
                    info.specs[key] = match[1].trim();
                }
            });
        }

        // Dajian uses #pane-description as the main container for product info
        const mainContainer = document.querySelector('#pane-description') ||
            document.querySelector('.product-info-tab') ||
            document.querySelector('.el-tab-pane');

        if (!mainContainer) {
            console.warn("⚠️ Product info container not found, using body scan results");
            return info;
        }

        // Find headers by text content
        const allDivs = Array.from(mainContainer.querySelectorAll('div, h2, h3, h4'));
        const specsHeader = allDivs.find(el => el.innerText && el.innerText.trim() === '产品规格');
        const featuresHeader = allDivs.find(el => el.innerText && el.innerText.trim() === '产品特点');

        // Extract Features (bullet points)
        if (featuresHeader) {
            console.log("✅ Found 产品特点 header");
            let sibling = featuresHeader.nextElementSibling;
            let attempts = 0;
            while (sibling && attempts < 5) {
                if (sibling.tagName === 'UL' || sibling.tagName === 'OL') {
                    sibling.querySelectorAll('li').forEach(li => {
                        const txt = li.innerText.trim();
                        if (txt.length > 10) info.features.push(txt);
                    });
                    break;
                } else if (sibling.querySelector('ul') || sibling.querySelector('ol')) {
                    sibling.querySelectorAll('li').forEach(li => {
                        const txt = li.innerText.trim();
                        if (txt.length > 10) info.features.push(txt);
                    });
                    break;
                }
                sibling = sibling.nextElementSibling;
                attempts++;
            }
        }

        // Extract Specs (key-value pairs from text)
        if (specsHeader) {
            console.log("✅ Found 产品规格 header");
            let sibling = specsHeader.nextElementSibling;
            let specsText = "";
            let attempts = 0;

            while (sibling && attempts < 10) {
                if (sibling.innerText && (sibling.innerText.includes('产品特点') || sibling.innerText.includes('图文描述'))) break;
                specsText += sibling.innerText + "\n";
                sibling = sibling.nextElementSibling;
                attempts++;
            }

            const lines = specsText.split("\n");
            lines.forEach(line => {
                if (line.includes(":") || line.includes("：")) {
                    const parts = line.split(/[:：]/);
                    if (parts.length >= 2) {
                        const key = parts[0].trim();
                        const val = parts.slice(1).join(":").trim();
                        const usefulKeys = ["Color", "颜色", "Material", "材质", "Product Type", "产品类型",
                            "Assembly", "组装", "Weight", "重量", "Dimension", "尺寸",
                            "Main Material", "主材质", "Item Code", "产品编号"];
                        if (usefulKeys.some(k => key.includes(k)) && val.length > 0 && val.length < 100) {
                            info.specs[key] = val;
                        }
                    }
                }
            });
        }

        // NEW: Extract Specs from Tables (Weight, Dimensions, Material)
        const tables = mainContainer.querySelectorAll('table');
        if (tables.length > 0) {
            console.log(`🔍 Scanning ${tables.length} table(s) for specs...`);

            tables.forEach(table => {
                const rows = table.querySelectorAll('tr');
                rows.forEach(row => {
                    const cells = Array.from(row.querySelectorAll('td, th'));

                    if (cells.length >= 2) {
                        const key = cells[0].innerText.trim();
                        const val = cells[1].innerText.trim();

                        // Key mapping (Chinese to English for standardization)
                        const keyMap = {
                            "重量": "Weight",
                            "产品重量": "Weight",
                            "净重": "Net Weight",
                            "毛重": "Gross Weight",
                            "长度": "Length",
                            "宽度": "Width",
                            "高度": "Height",
                            "尺寸": "Dimensions",
                            "产品尺寸": "Product Dimensions",
                            "包装尺寸": "Package Dimensions",
                            "材质": "Material",
                            "主材质": "Main Material",
                            "颜色": "Color",
                            "产品类型": "Product Type"
                        };

                        // Check if key matches any useful spec
                        const matchedKey = keyMap[key] || key;
                        const specKeys = ["Weight", "Length", "Width", "Height", "Dimensions",
                            "Material", "Color", "Product Type", "Net Weight", "Gross Weight"];

                        if (specKeys.some(k => matchedKey.includes(k) || key.includes(k)) && val.length > 0 && val.length < 100) {
                            // Use standardized English key if available
                            info.specs[matchedKey] = val;
                        }
                    }
                });
            });
        }

        console.log(`📋 Extracted: ${info.features.length} features, ${Object.keys(info.specs).length} specs`);
        return info;
    }

    // --- Main Extraction Logic ---
    async function extractData() {
        // Collect Media first (might involve interaction)
        const videos = await extractVideoInteractive();
        const imgs = scanForImages();
        const prodInfo = extractProductInfo();

        // IMPROVED: More robust title extraction with multiple fallback strategies
        let title = getElementText(SELECTORS.title);
        
        // Fallback 1: Try to get title from page title (before the dash/pipe)
        if (!title || title.length < 8) {
            const pageTitle = document.title;
            // Common patterns: "Product Name - Site Name" or "Product Name | Site Name"
            const pageTitleMatch = pageTitle.match(/^([^|\-–]+)/);
            if (pageTitleMatch) {
                const candidate = pageTitleMatch[1].trim();
                if (candidate.length > 8 && !isLikelyVariantName(candidate)) {
                    title = candidate;
                    console.log(`📝 Title from page title: ${title}`);
                }
            }
        }
        
        // Fallback 2: Try to find title in breadcrumb (last item)
        if (!title || title.length < 8) {
            const breadcrumb = document.querySelector('.breadcrumb li:last-child, .el-breadcrumb__item:last-child');
            if (breadcrumb) {
                const candidate = breadcrumb.innerText.trim();
                if (candidate.length > 8 && !isLikelyVariantName(candidate)) {
                    title = candidate;
                    console.log(`📝 Title from breadcrumb: ${title}`);
                }
            }
        }
        
        // Fallback 3: Try to get from Open Graph meta tag
        if (!title || title.length < 8) {
            const ogTitle = document.querySelector('meta[property="og:title"]');
            if (ogTitle && ogTitle.content) {
                const candidate = ogTitle.content.trim();
                if (candidate.length > 8) {
                    title = candidate;
                    console.log(`📝 Title from og:title: ${title}`);
                }
            }
        }
        
        // Last resort
        if (!title || title.length < 5) {
            title = "Unknown Product - Please Check";
            console.warn("⚠️ Could not extract product title!");
        }

        let sku = getElementText(SELECTORS.sku);
        if (sku) {
            sku = sku.replace("Item Code:", "").replace("SKU:", "").replace(/\s+/g, "").trim();
        } else {
            const match = document.body.innerText.match(/Item Code:\s*([A-Za-z0-9/-]+)/);
            sku = match ? match[1].replace(/\s+/g, "").trim() : `UNK-${Date.now()}`;
        }

        let price = 0;
        let shipping = 0;
        const bodyText = document.body.innerText;

        const potentialPrices = Array.from(document.querySelectorAll("*"))
            .filter(e => e.innerText && e.innerText.includes("$") && e.children.length === 0 && e.offsetParent !== null)
            .map(e => parseFloat(e.innerText.replace(/[^0-9.]/g, "")))
            .filter(v => v > 10 && v < 10000);
        if (potentialPrices.length > 0) price = potentialPrices[0];

        // Enhanced shipping/fulfillment fee extraction for Dajian/GigaCloud
        // Priority order: Fulfillment Fee > Estimated Shipping > 预估物流费
        
        // 1. Look for "Estimated Fulfillment Fee: $XX.XX ~ $YY.YY" pattern (take higher value for safety)
        const fulfillmentMatch = bodyText.match(/Estimated Fulfillment Fee[:\s]*\$?([\d.]+)\s*[~-]\s*\$?([\d.]+)/i);
        if (fulfillmentMatch) {
            // Take the higher value for conservative pricing
            const low = parseFloat(fulfillmentMatch[1]);
            const high = parseFloat(fulfillmentMatch[2]);
            shipping = Math.max(low, high);
            console.log(`🚚 Found Fulfillment Fee: $${low} ~ $${high}, using $${shipping}`);
        }
        
        // 2. Fallback: Single fulfillment fee value
        if (shipping === 0) {
            const singleFulfillment = bodyText.match(/Fulfillment Fee[:\s]*\$?([\d.]+)/i);
            if (singleFulfillment) {
                shipping = parseFloat(singleFulfillment[1]);
                console.log(`🚚 Found single Fulfillment Fee: $${shipping}`);
            }
        }
        
        // 3. Fallback: Chinese format
        if (shipping === 0) {
            const shipMatch = bodyText.match(/预估物流费[:\s]*\$?([\d.]+)/);
            if (shipMatch) {
                shipping = parseFloat(shipMatch[1]);
                console.log(`🚚 Found 预估物流费: $${shipping}`);
            }
        }
        
        // 4. Fallback: Estimated Shipping
        if (shipping === 0) {
            const estShipMatch = bodyText.match(/Estimated Shipping[:\s]*\$?([\d.]+)/i);
            if (estShipMatch) {
                shipping = parseFloat(estShipMatch[1]);
                console.log(`🚚 Found Estimated Shipping: $${shipping}`);
            }
        }
        
        console.log(`💰 Final extracted: Price=$${price}, Shipping=$${shipping}`);

        // Variants - record variant info but DO NOT append to SKU
        // eBay has SKU length limits and appending variant names causes issues
        const attributes = {};
        const specs = {};
        let variantStr = "";
        for (const sel of SELECTORS.activeVariant) {
            const activeOptions = document.querySelectorAll(sel);
            activeOptions.forEach(opt => {
                let category = "Variant";
                const group = opt.parentElement.parentElement;
                if (group && group.innerText.includes(":")) category = group.innerText.split("\n")[0].replace(":", "").trim();
                const val = opt.innerText.trim();
                attributes[category] = val;
                // DO NOT append to SKU - just record for display
                variantStr += ` - ${val}`;
            });
            if (activeOptions.length > 0) break;
        }

        // Split product attributes from package/shipping specs
        Object.entries(prodInfo.specs).forEach(([key, value]) => {
            if (!value) return;
            if (
                key.startsWith("Package ") ||
                key === "Gross Weight" ||
                key === "Shipping Weight" ||
                key === "Package Dimensions"
            ) {
                specs[key] = value;
            } else {
                attributes[key] = value;
            }
        });

        // Description
        let finalDescription = "";
        const descContainer = document.querySelector("#pane-description") ||
            document.querySelector("#description") ||
            document.querySelector(".product-detail-content") ||
            document.querySelector(".product-description") ||
            document.querySelector(".el-tab-pane");
        if (descContainer) finalDescription += descContainer.innerHTML;
        if (prodInfo.features.length > 0) finalDescription += `<br><hr><h3>Product Features</h3><ul>${prodInfo.features.map(f => `<li>${f}</li>`).join("")}</ul>`;
        if (Object.keys(prodInfo.specs).length > 0) finalDescription += `<br><h3>Specifications</h3><ul>${Object.entries(prodInfo.specs).map(([k, v]) => `<li><strong>${k}:</strong> ${v}</li>`).join("")}</ul>`;
        if (!finalDescription.trim()) {
            const metaDesc = document.querySelector('meta[name="description"], meta[property="og:description"]');
            if (metaDesc && metaDesc.content) {
                finalDescription = `<p>${escapeHtml(metaDesc.content.trim())}</p>`;
            }
        }

        return {
            sku: sku,  // Use original SKU without variant suffix
            title: title + variantStr,
            price: price,
            shipping: shipping,
            stock: 99,
            description: finalDescription,
            images: imgs,
            videos: videos,
            attributes: attributes,
            specs: specs,
            url: window.location.href
        };
    }

    // --- Auto-Favorite (心形收藏按钮) ---
    function autoClickFavorite() {
        // GigaCloud 专用: 精确选择器 (基于诊断确认)
        // 按钮: button.btn-wishlist
        // 未收藏图标: i.gc-like
        // 已收藏图标: i.gc-like-full
        // 已收藏父容器: div.active-wishlist

        // 策略1: 直接找 btn-wishlist 按钮
        let btn = document.querySelector('button.btn-wishlist');
        if (!btn) btn = document.querySelector('.btn-wishlist');
        if (!btn) btn = document.querySelector('[class*="btn-wishlist"]');

        // 策略2: 找 gc-like 图标，回溯到父按钮
        if (!btn) {
            const icons = document.querySelectorAll('i.gc-like, i[class*="gc-like"]');
            for (const icon of icons) {
                const parent = icon.closest('button') || icon.parentElement;
                if (parent && parent.offsetParent) {
                    const r = parent.getBoundingClientRect();
                    if (r.y > 0 && r.y < 800 && r.width > 10) {
                        btn = parent;
                        break;
                    }
                }
            }
        }

        if (!btn) {
            // 策略3: 通用关键词搜索 (非 GigaCloud 页面的兜底)
            const keywords = ['heart', 'collect', 'favorite', 'wish'];
            for (const kw of keywords) {
                const els = document.querySelectorAll(`[class*="${kw}"]`);
                for (const el of els) {
                    if (!el.offsetParent || el.offsetWidth < 5) continue;
                    btn = el;
                    break;
                }
                if (btn) break;
            }
        }

        if (!btn) {
            console.log("❤️ 未找到收藏按钮");
            return false;
        }

        // 检查是否已收藏
        const icon = btn.querySelector('i');
        if (icon) {
            const cls = (icon.className || '').toString();
            if (cls.includes('gc-like-full') || cls.includes('liked') || cls.includes('filled')) {
                console.log("❤️ 已收藏，无需再次点击");
                return true;
            }
        }
        // 检查 active-wishlist 父容器
        let parent = btn.parentElement;
        for (let i = 0; i < 3 && parent; i++) {
            if (parent.className && parent.className.toString().includes('active-wishlist')) {
                console.log("❤️ 已收藏 (active-wishlist)，无需再次点击");
                return true;
            }
            parent = parent.parentElement;
        }

        // 点击收藏
        console.log("❤️ 自动点击收藏按钮:", btn.className);
        btn.click();

        // 1秒后关闭弹出的群组选择弹窗
        setTimeout(() => {
            try {
                // 按 Escape 关闭弹窗
                document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
                // 点击空白区域关闭
                document.body.click();
            } catch (e) {
                // ignore
            }
        }, 1500);

        return true;
    }

    // --- UI Injection ---
    function injectButton() {
        if (document.getElementById("dajian-collect-btn")) return;

        const btn = document.createElement("button");
        btn.id = "dajian-collect-btn";
        btn.innerHTML = "<span>🐟</span> 采集到 Dashboard";
        btn.style.cssText = `
            position: fixed; bottom: 80px; right: 30px; z-index: 2147483647;
            background: linear-gradient(135deg, #FF6B6B, #EE5253); color: white;
            border: 2px solid white; padding: 12px 24px; border-radius: 50px;
            font-weight: bold; font-size: 16px; box-shadow: 0 5px 20px rgba(0,0,0,0.3);
            cursor: pointer; transition: transform 0.2s; font-family: sans-serif;
            display: flex; align-items: center; gap: 8px;
        `;

        btn.onmouseover = () => btn.style.transform = "scale(1.05)";
        btn.onmouseout = () => btn.style.transform = "scale(1)";
        btn.onclick = handleCollect;

        document.body.appendChild(btn);
    }

    async function handleCollect() {
        const btn = document.getElementById("dajian-collect-btn");
        const originalText = btn.innerHTML;

        try {
            btn.innerHTML = "👀 正在点击视频...";
            btn.style.background = "#666";

            // Wait for extraction (now async)
            const data = await extractData();
            console.log("📦 Payload:", data);

            btn.innerHTML = "🚀 发送中...";

            // IMPROVED: Comprehensive validation before sending
            let warnings = [];
            let criticalErrors = [];
            const plainDescription = (data.description || "")
                .replace(/<[^>]+>/g, " ")
                .replace(/\s+/g, " ")
                .trim();
            
            // Critical checks (will show confirmation)
            if (data.price === 0) criticalErrors.push("❌ 价格未找到");
            if (data.title.includes("Unknown") || data.title.length < 10) {
                criticalErrors.push("❌ 标题无效: " + data.title);
            }
            if (data.images.length <= 1) criticalErrors.push(`❌ 图片过少: ${data.images.length} 张`);
            if (!plainDescription) criticalErrors.push("❌ 无描述内容");
            
            // Warning checks (informational only)
            if (data.videos.length === 0) warnings.push("⚠️ 无视频");
            if (data.shipping === 0) warnings.push("⚠️ 运费未找到");
            if (Object.keys(data.attributes).length === 0) warnings.push("⚠️ 无规格信息");
            if (plainDescription && plainDescription.length < 80) warnings.push(`⚠️ 描述过短: ${plainDescription.length} 字符`);
            
            // Show confirmation for critical errors
            if (criticalErrors.length > 0) {
                const allIssues = [...criticalErrors, ...warnings].join("\n");
                const diagnostics = [
                    `SKU: ${data.sku}`,
                    `图片: ${data.images.length}`,
                    `视频: ${data.videos.length}`,
                    `描述字符: ${plainDescription.length}`,
                    `URL: ${window.location.href}`
                ].join("\n");
                console.warn("⚠️ Collection diagnostics", { data, plainDescriptionLength: plainDescription.length });
                if (!confirm(`⚠️ 数据可能有误:\n${allIssues}\n\n诊断信息:\n${diagnostics}\n\n确定要继续采集吗?`)) {
                    throw new Error("用户取消");
                }
            }

            chrome.runtime.sendMessage({ action: "collectProduct", data: data }, (response) => {
                if (chrome.runtime.lastError) {
                    showToast("⛔ " + chrome.runtime.lastError.message, "error");
                    return;
                }
                if (response && response.status === "success") {
                    showToast(`✅ 已采集!\n📸 图片: ${data.images.length} | 🎥 视频: ${data.videos.length}\n🚚 运费: $${data.shipping}`, "success");
                    // 自动点击收藏按钮 (心形图标)
                    setTimeout(() => {
                        try {
                            const favResult = autoClickFavorite();
                            if (favResult) {
                                console.log("❤️ 自动收藏完成");
                            }
                        } catch (e) {
                            console.warn("❤️ 自动收藏失败:", e);
                        }
                    }, 500);
                } else {
                    const errorMsg = response && response.message ? response.message : "Unknown error";
                    showToast("❌ 失败: " + errorMsg, "error");
                }
                setTimeout(() => {
                    btn.innerHTML = originalText;
                    btn.style.background = "linear-gradient(135deg, #FF6B6B, #EE5253)";
                }, 3000);
            });

        } catch (e) {
            console.error("采集错误:", e);
            showToast("⛔ " + e.message, "error");
            btn.innerHTML = originalText;
            btn.style.background = "linear-gradient(135deg, #FF6B6B, #EE5253)";
        }
    }

    function showToast(msg, type) {
        const div = document.createElement("div");
        div.innerText = msg;
        div.style.cssText = `
            position: fixed; top: 20px; left: 50%; transform: translateX(-50%);
            padding: 10px 20px; border-radius: 20px; color: white;
            background: ${type === 'success' ? '#28a745' : '#dc3545'};
            z-index: 999999;font-weight:bold; box-shadow: 0 2px 10px rgba(0,0,0,0.2); white-space: pre-line; text-align: center;
        `;
        document.body.appendChild(div);
        setTimeout(() => div.remove(), 4000);
    }

    injectButton();
    setInterval(injectButton, 2000);

})();
