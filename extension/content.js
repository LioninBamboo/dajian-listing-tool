// content.js - Robust Version 4.0 (Interactive Video + Deep Images)
(() => {
    console.log("🚀 AquaVerve Collector v4.0 Starting...");

    // --- Selectors Configuration ---
    const SELECTORS = {
        title: ["h1", ".product-title", ".right_info h1", "#product-name"],
        sku: ["#itemCode", ".sku", ".product-code", ".item-code", ".pro_id"],
        price: [".price-valid", ".product-price", ".price", ".p-price span"],
        activeVariant: [".options-item-active", ".spec-item.selected", ".attr-item.active", ".pro_spec li.active"],
        images: [".swiper-slide img", "#pro_big_img img", ".gallery-thumbs img"],
        // Video trigger buttons (thumbnails with play icon)
        videoTrigger: [".el-icon-video-play", ".gc-video", ".gc-video-a", ".video-thumbnail", ".image-item .icon-play"]
    };

    function getElementText(selectorList) {
        for (const sel of selectorList) {
            const el = document.querySelector(sel);
            if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
        }
        return null;
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

        // Regex (Deep scan for mp4 strings in HTML)
        const html = document.body.innerHTML;
        const absMatches = html.match(/https?:\/\/[^"'\s<>]+\.mp4/gi);
        if (absMatches) absMatches.forEach(addUrl);

        // Relative Regex
        const relMatches = html.match(/['"](\/[^"'\s<>]+\.mp4)['"]/gi);
        if (relMatches) {
            relMatches.forEach(m => addUrl(m.replace(/['"]/g, "")));
        }

        return Array.from(videoUrls);
    }


    // --- 2. Image Scanning (Enhanced) ---
    function scanForImages() {
        const imageUrls = new Set();

        const addUrl = (u, priority = false) => {
            if (!u) return;
            u = u.trim();
            if (u.startsWith("//")) u = "https:" + u;
            if (u.startsWith("/")) u = window.location.origin + u;

            if (u.startsWith("http")) {
                // Filter out common junk
                if (u.includes("logo") || u.includes("icon") ||
                    u.includes("empty") || u.includes("placeholder") ||
                    u.includes("product_base") || u.includes("default")) {
                    return;
                }

                // Priority images go first (carousel images)
                if (priority) {
                    imageUrls.add(u);
                } else {
                    imageUrls.add(u);
                }
            }
        };

        // A. HIGH PRIORITY: Main product carousel images
        const carouselImages = document.querySelectorAll('.el-carousel__item img, .swiper-slide img, #pro_big_img img');
        carouselImages.forEach(img => {
            // Try multiple sources
            const src = img.getAttribute('data-original') ||
                img.getAttribute('data-src') ||
                img.src;
            if (src && (img.naturalWidth > 200 || img.width > 200 || src.includes('b2bfiles'))) {
                addUrl(src, true);
            }
        });

        // B. Gallery thumbnails
        document.querySelectorAll('.gallery-thumbs img, .product-images img').forEach(img => {
            const src = img.getAttribute('data-original') || img.src;
            addUrl(src, true);
        });

        // C. Deep Scan (all other images)
        document.querySelectorAll("img").forEach(img => {
            const strategies = [
                img.getAttribute("data-original"),
                img.getAttribute("data-src"),
                img.getAttribute("lazy-src"),
                img.src
            ];
            strategies.forEach(src => {
                if (src) {
                    let fullSrc = src.trim();
                    if (fullSrc.startsWith("//")) fullSrc = "https:" + fullSrc;
                    else if (fullSrc.startsWith("/")) fullSrc = window.location.origin + fullSrc;

                    // Only add if it's a reasonable size
                    if (fullSrc.startsWith("http") &&
                        (img.naturalWidth > 200 || img.width > 200 || fullSrc.includes("big") || fullSrc.includes("b2bfiles"))) {
                        addUrl(fullSrc);
                    }
                }
            });
        });

        // Return unique images, limit to 48
        return Array.from(imageUrls).slice(0, 48);
    }

    // --- 3. Tab & Product Info Extraction ---
    function extractProductInfo() {
        const info = { features: [], specs: {} };

        // Dajian uses #pane-description as the main container for product info
        const mainContainer = document.querySelector('#pane-description') ||
            document.querySelector('.product-info-tab') ||
            document.querySelector('.el-tab-pane');

        if (!mainContainer) {
            console.warn("⚠️ Product info container not found");
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

        // Basic Info
        let title = getElementText(SELECTORS.title);
        if (!title) { title = document.title.split("-")[0].trim(); if (title.length < 5) title = "Unknown Title"; }

        let sku = getElementText(SELECTORS.sku);
        if (sku) { sku = sku.replace("Item Code:", "").replace("SKU:", "").trim(); }
        else { const match = document.body.innerText.match(/Item Code:\s*([A-Za-z0-9-]+)/); sku = match ? match[1] : `UNK-${Date.now()}`; }

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

        // Variants
        const attributes = {};
        let skuSuffix = "";
        let variantStr = "";
        for (const sel of SELECTORS.activeVariant) {
            const activeOptions = document.querySelectorAll(sel);
            activeOptions.forEach(opt => {
                let category = "Variant";
                const group = opt.parentElement.parentElement;
                if (group && group.innerText.includes(":")) category = group.innerText.split("\n")[0].replace(":", "").trim();
                const val = opt.innerText.trim();
                attributes[category] = val;
                skuSuffix += `-${val.replace(/[^a-zA-Z0-9]/g, "")}`;
                variantStr += ` - ${val}`;
            });
            if (activeOptions.length > 0) break;
        }

        // Merge Specs
        Object.assign(attributes, prodInfo.specs);

        // Description
        let finalDescription = "";
        const descContainer = document.querySelector("#description") || document.querySelector(".product-detail-content");
        if (descContainer) finalDescription += descContainer.innerHTML;
        if (prodInfo.features.length > 0) finalDescription += `<br><hr><h3>Product Features</h3><ul>${prodInfo.features.map(f => `<li>${f}</li>`).join("")}</ul>`;
        if (Object.keys(prodInfo.specs).length > 0) finalDescription += `<br><h3>Specifications</h3><ul>${Object.entries(prodInfo.specs).map(([k, v]) => `<li><strong>${k}:</strong> ${v}</li>`).join("")}</ul>`;

        return {
            sku: sku + skuSuffix,
            title: title + variantStr,
            price: price,
            shipping: shipping,
            stock: 99,
            description: finalDescription,
            images: imgs,
            videos: videos,
            attributes: attributes,
            url: window.location.href
        };
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

            // Validation
            let warnings = [];
            if (data.price === 0) warnings.push("❌ 价格未找到");
            if (data.videos.length === 0) warnings.push("⚠️ 无视频");
            // Only confirm if critical price missing
            if (data.price === 0) {
                if (!confirm(`⚠️ 数据可能有误:\n${warnings.join("\n")}\n\n继续?`)) throw new Error("用户取消");
            }

            chrome.runtime.sendMessage({ action: "collectProduct", data: data }, (response) => {
                if (chrome.runtime.lastError) {
                    showToast("⛔ " + chrome.runtime.lastError.message, "error");
                    return;
                }
                if (response && response.status === "success") {
                    showToast(`✅ 已采集!\n📸 图片: ${data.images.length} | 🎥 视频: ${data.videos.length}\n🚚 运费: $${data.shipping}`, "success");
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
