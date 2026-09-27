(function() {
    var scripts = document.getElementsByTagName("script");
    var thisScript = scripts[scripts.length - 1];
    var srcUrl = new URL(thisScript.src);
    var widgetId = srcUrl.searchParams.get("id");
    var apiOrigin = srcUrl.origin;

    if (!widgetId) {
        console.error("Widget script: no widget id found in script src");
        return;
    }

    fetch(apiOrigin + "/widgets/" + widgetId + "/config")
        .then(function(res) { return res.json(); })
        .then(function(config) {
            if (evaluateTargeting(config.targeting_rules || {})) {
                applyDelay(function() { renderWidget(config); }, config.targeting_rules.delay_seconds);
            }
        })
        .catch(function(err) { console.error("Widget failed to load config:", err); });

    function evaluateTargeting(rules) {
        if (rules.page_paths && rules.page_paths.length > 0) {
            var pathname = window.location.pathname;
            var matched = false;
            for (var i = 0; i < rules.page_paths.length; i++) {
                if (matchPath(rules.page_paths[i], pathname)) { matched = true; break; }
            }
            if (!matched) return false;
        }
        if (rules.once_per_visitor) {
            if (localStorage.getItem("widget_seen_" + widgetId)) return false;
        }
        return true;
    }

    function matchPath(pattern, pathname) {
        if (pattern === pathname) return true;
        if (pattern.endsWith("/*")) {
            var prefix = pattern.slice(0, -2);
            return pathname === prefix || pathname.startsWith(prefix + "/");
        }
        if (pattern === "*") return true;
        return false;
    }

    function applyDelay(callback, seconds) {
        if (!seconds || seconds <= 0) { callback(); return; }
        setTimeout(callback, seconds * 1000);
    }

    function markSeen() {
        try { localStorage.setItem("widget_seen_" + widgetId, "1"); } catch (e) {}
    }

    function leadingZeroBits(byteArray) {
        var leading = 0;
        for (var i = 0; i < byteArray.length; i++) {
            var byte = byteArray[i];
            if (byte === 0) { leading += 8; }
            else { leading += 8 - (32 - Math.clz32(byte)); break; }
        }
        return leading;
    }

    async function solveChallenge(apiOrigin, widgetId) {
        var chRes = await fetch(apiOrigin + "/challenge?widget_id=" + encodeURIComponent(widgetId));
        var ch = await chRes.json();
        if (!ch.difficulty) return "";
        if (!(crypto && crypto.subtle)) {
            console.warn("Widget: proof-of-work needs a secure context (HTTPS or localhost); submitting without proof.");
            return "";
        }
        var encoder = new TextEncoder();
        for (var nonce = 0; nonce < 1048576; nonce++) {
            var digest = await crypto.subtle.digest("SHA-256", encoder.encode(ch.challenge + ":" + nonce));
            if (leadingZeroBits(new Uint8Array(digest)) >= ch.difficulty) return String(nonce);
        }
        throw new Error("could not solve bot check challenge");
    }

    function renderWidget(config) {
        var host = document.createElement("div");
        host.style.cssText = "all:initial; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;";

        var shadow = host.attachShadow({ mode: "open" });

        var style = document.createElement("style");
        style.textContent = [
            ":host { all:initial; }",
            "* { box-sizing: border-box; margin: 0; padding: 0; }",
            ".widget { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;",
            "  padding: 24px; max-width: 340px; width: 100%;",
            "  box-shadow: 0 4px 24px rgba(0,0,0,0.08);",
            "  animation: slideUp 0.3s ease-out; }",
            "@keyframes slideUp { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:translateY(0); } }",
            ".title { font-size: 17px; font-weight: 600; color: #1a202c; margin-bottom: 16px; }",
            ".field { margin-bottom: 14px; }",
            ".label { display: block; font-size: 13px; font-weight: 500; color: #4a5568; margin-bottom: 5px; }",
            ".input { width: 100%; padding: 9px 12px; border: 1px solid #e2e8f0; border-radius: 8px;",
            "  font-size: 14px; color: #1a202c; background: #f7fafc;",
            "  transition: border-color 0.15s, box-shadow 0.15s; outline: none; }",
            ".input:focus { border-color: #4299e1; box-shadow: 0 0 0 3px rgba(66,153,225,0.15); background: #fff; }",
            ".input::placeholder { color: #a0aec0; }",
            ".consent { display: flex; align-items: flex-start; gap: 8px; margin: 14px 0;",
            "  font-size: 12px; color: #718096; line-height: 1.4; }",
            ".consent input[type=checkbox] { margin-top: 2px; flex-shrink: 0; accent-color: #4299e1; }",
            ".btn { width: 100%; padding: 10px 16px; border: none; border-radius: 8px;",
            "  font-size: 14px; font-weight: 600; cursor: pointer;",
            "  color: #fff; background: linear-gradient(135deg, #4299e1, #3182ce);",
            "  transition: transform 0.1s, box-shadow 0.15s, opacity 0.15s; }",
            ".btn:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(66,153,225,0.35); }",
            ".btn:active { transform: translateY(0); }",
            ".btn:focus-visible { outline: 2px solid #4299e1; outline-offset: 2px; }",
            ".btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; box-shadow: none; }",
            ".btn .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.3);",
            "  border-top-color: #fff; border-radius: 50%; animation: spin 0.6s linear infinite;",
            "  vertical-align: middle; margin-right: 6px; }",
            "@keyframes spin { to { transform: rotate(360deg); } }",
            ".status { margin-top: 10px; font-size: 13px; line-height: 1.4; min-height: 20px; }",
            ".status.success { color: #38a169; }",
            ".status.error { color: #e53e3e; }",
            ".success-box { text-align: center; padding: 20px 0 8px; }",
            ".success-icon { font-size: 36px; margin-bottom: 8px; }",
            ".success-text { font-size: 14px; color: #38a169; font-weight: 500; }",
            ".success-sub { font-size: 12px; color: #718096; margin-top: 4px; }",
        ].join("\n");

        shadow.appendChild(style);

        var container = document.createElement("div");
        container.className = "widget";

        var title = document.createElement("div");
        title.className = "title";
        title.textContent = config.title;
        container.appendChild(title);

        var form = document.createElement("form");
        form.setAttribute("aria-label", config.title);

        (config.fields || []).forEach(function(field) {
            var fieldDiv = document.createElement("div");
            fieldDiv.className = "field";

            var label = document.createElement("label");
            label.className = "label";
            label.textContent = field.label;
            label.setAttribute("for", "w-" + field.name);

            var input = document.createElement("input");
            input.className = "input";
            input.type = field.type || "text";
            input.name = field.name;
            input.id = "w-" + field.name;
            input.required = !!field.required;
            input.setAttribute("aria-required", field.required ? "true" : "false");
            if (field.placeholder) input.placeholder = field.placeholder;

            fieldDiv.appendChild(label);
            fieldDiv.appendChild(input);
            form.appendChild(fieldDiv);
        });

        var honeypot = document.createElement("input");
        honeypot.type = "text";
        honeypot.name = "website";
        honeypot.tabIndex = -1;
        honeypot.autocomplete = "off";
        honeypot.style.cssText = "position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden;";
        form.appendChild(honeypot);

        var consentDiv = document.createElement("div");
        consentDiv.className = "consent";
        var consentCheckbox = document.createElement("input");
        consentCheckbox.type = "checkbox";
        consentCheckbox.name = "consent_given";
        consentCheckbox.required = true;
        consentCheckbox.id = "w-consent";
        var consentText = document.createElement("label");
        consentText.setAttribute("for", "w-consent");
        consentText.textContent = "I agree to the privacy policy and consent to data processing";
        consentDiv.appendChild(consentCheckbox);
        consentDiv.appendChild(consentText);
        form.appendChild(consentDiv);

        var submitBtn = document.createElement("button");
        submitBtn.type = "submit";
        submitBtn.className = "btn";
        submitBtn.textContent = config.button_text || "Submit";
        form.appendChild(submitBtn);

        var statusMsg = document.createElement("div");
        statusMsg.className = "status";
        form.appendChild(statusMsg);

        form.addEventListener("submit", function(e) {
            e.preventDefault();
            var formData = new FormData(form);
            var data = {};
            var consent = false;
            formData.forEach(function(value, key) {
                if (key === "consent_given") consent = true;
                else if (key !== "website") data[key] = value;
            });

            submitBtn.disabled = true;
            submitBtn.innerHTML = '<span class="spinner"></span>Submitting...';
            statusMsg.textContent = "";
            statusMsg.className = "status";

            solveChallenge(apiOrigin, widgetId)
                .then(function(proof) {
                    return fetch(apiOrigin + "/submissions", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            widget_id: parseInt(widgetId, 10),
                            data: data,
                            website: formData.get("website") || "",
                            proof: proof || "",
                            consent_given: consent,
                        }),
                    });
                })
                .then(function(res) {
                    if (res.status === 429) {
                        statusMsg.textContent = "Too many requests — please wait a moment and try again.";
                        statusMsg.className = "status error";
                        resetBtn();
                        return;
                    }
                    if (!res.ok) {
                        statusMsg.textContent = "Something went wrong. Please check your input.";
                        statusMsg.className = "status error";
                        resetBtn();
                        return;
                    }
                    markSeen();
                    form.style.display = "none";
                    var successBox = document.createElement("div");
                    successBox.className = "success-box";
                    successBox.innerHTML = '<div class="success-icon">&#10003;</div>' +
                        '<div class="success-text">Thanks! Please check your email.</div>' +
                        '<div class="success-sub">We sent a confirmation link to verify your subscription.</div>';
                    container.appendChild(successBox);
                })
                .catch(function() {
                    statusMsg.textContent = "Network error. Please try again.";
                    statusMsg.className = "status error";
                    resetBtn();
                });

            function resetBtn() {
                submitBtn.disabled = false;
                submitBtn.textContent = config.button_text || "Submit";
            }
        });

        container.appendChild(form);
        shadow.appendChild(container);
        thisScript.parentNode.insertBefore(host, thisScript.nextSibling);
    }
})();
