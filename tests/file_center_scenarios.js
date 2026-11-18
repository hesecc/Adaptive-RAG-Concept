// Interact with the shipped HTML and scripts; observe the emitted HTTP requests.
(async () => {
  const id = name => document.getElementById(name);
  const one = s => document.querySelector(s);
  const btn = (root, text) => [...root.querySelectorAll("button")].find(b => b.textContent === text);
  const assert = (value, label) => { if (!value) throw Error(label); };
  const until = async (fn, label) => { for (let i=0;i<150;i++) { if (fn()) return; await new Promise(r=>setTimeout(r,10)); } throw Error(label); };
  const clickRow = (title, label) => btn(btn(id("file-list"), title).closest("article"), label).click();
  const close = modal => modal.querySelector("[data-close]").click();
  const detail = async () => { btn(id("file-list"), "Guide").click(); await until(()=>one("#detail-body pre"), "preview"); };
  const select = (name, value) => { id(name).value = value; id(name).dispatchEvent(new Event("change")); };
  async function submit(url) {
    const n = writes.length;
    id("editor-form").requestSubmit();
    await until(()=>writes.length > n && !id("file-editor").open && !fileCenter.state.busy, "save: " + url + " " + id("editor-error").textContent);
    assert(writes[n].url === url, "wrong endpoint: " + writes[n].url);
    return writes[n].body;
  }
  try {
    await until(()=>btn(id("file-list"), "Guide"), "load list");
    assert(reads.includes("/files?view=" + (adminFixture ? "all" : "mine")), "role list");
    assert(id("evaluation-open").hidden === !adminFixture, "evaluation permission");
    assert(id("file-list").textContent.includes("Alice") && id("file-list").textContent.includes("Editor"), "display names");
    assert(fileCenter.time(1700000000) === fileCenter.time("2023-11-14T22:13:20Z"), "epoch / ISO");
    id("file-kind").value = "image"; id("file-kind").dispatchEvent(new Event("input"));
    assert(!btn(id("file-list"), "Guide") && btn(id("file-list"), "Photo"), "image alias filter");
    id("file-kind").value = ""; id("file-kind").dispatchEvent(new Event("input"));
    btn(id("file-list"), "Photo").click();
    await until(()=>one("#detail-body h3"), "image history");
    assert(id("detail-body").textContent.includes("v1") && !one("#detail-body select"), "historical images have no preview selector");
    assert(reads.includes("/files/image/pic/content?revision=r2") && !reads.includes("/files/image/pic/content?revision=r1"), "current image revision only");
    close(id("file-detail"));
    await detail();
    assert(one("#detail-body pre").textContent === "<b>plain text</b>" && !one("#detail-body pre b"), "inert text preview");
    const rev = one("#detail-body select"); rev.value = "r1"; rev.dispatchEvent(new Event("change"));
    assert(one("#detail-body a").getAttribute("href") === "/files/document/guide/content?original=true&revision=r1", "revision original");
    assert(id("detail-body").textContent.includes("updated"), "events");
    if (adminFixture) {
      btn(id("detail-body"), "发布版本 v2").click(); await until(()=>!id("file-detail").open && !fileCenter.state.busy, "publish");
      assert(writes.at(-1).url === "/publish" && writes.at(-1).body.expected_generation === "g1", "publish contract");
      await detail(); btn(id("detail-body"), "保存分类").click(); await until(()=>!id("file-detail").open && !fileCenter.state.busy, "access");
      assert(writes.at(-1).url === "/documents/access" && !('allowed_subjects' in writes.at(-1).body), "preserve ACL");
    } else {
      assert(!btn(id("detail-body"), "保存分类"), "member access controls"); close(id("file-detail"));
      id("file-add").click();
      assert(id("editor-domains").querySelectorAll("input").length === 1 && !one("#editor-domains input:checked"), "member classifications");
      id("editor-title").value = "New"; id("editor-content").value = "Text";
      const doc = await submit("/files/documents"); assert(!("data_domains" in doc || "allowed_subjects" in doc), "default unclassified");
      id("file-add").click(); select("editor-kind", "pdf");
      const transfer = new DataTransfer(); transfer.items.add(new File(["%PDF-fixture"], "local.pdf", {type:"application/pdf"}));
      id("editor-upload").files = transfer.files; id("editor-title").value = "PDF";
      one("#editor-domains input").checked = true;
      const pdf = await submit("/files/pdf"); assert(atob(pdf.content_base64) === "%PDF-fixture" && pdf.data_domains[0] === "public", "PDF body");
      id("file-add").click(); select("editor-kind", "image"); assert(id("editor-domain-fields").hidden, "private image form");
      const picture = new DataTransfer(); picture.items.add(new File(["image"], "p.png", {type:"image/png"}));
      id("editor-upload").files = picture.files; id("editor-title").value = "Image";
      const img = await submit("/files/images"); assert(!("data_domains" in img || "allowed_subjects" in img), "private image body");
      clickRow("Photo", "更新"); await until(()=>id("file-editor").open && !id("editor-save").disabled, "edit image");
      assert(id("editor-description").value === "Caption", "image prefill");
      const edit = await submit("/files/images/pic"); assert(writes.at(-1).method === "PATCH" && edit.expected_revision === "r2" && !("allowed_subjects" in edit || "data_domains" in edit), "image update");
      clickRow("Guide", "更新"); await until(()=>id("file-editor").open && !id("editor-save").disabled, "edit document");
      assert(id("editor-content").value === "<b>plain text</b>" && id("editor-domain-fields").hidden, "document prefill");
      id("editor-version").value = "v3"; const updated = await submit("/files/documents");
      assert(updated.document_id === "guide" && updated.version === "v3" && !("data_domains" in updated), "document update");
      const count = writes.length; window.confirm = () => false; clickRow("Guide", "移入回收站"); assert(writes.length === count, "cancel delete");
      await Promise.resolve(); window.confirm = () => true; clickRow("Guide", "移入回收站"); await until(()=>!btn(id("file-list"), "Guide"), "trash");
      assert(writes.at(-1).body.expected_revision === "r2" && writes.at(-1).body.expected_generation === "g1", "trash contract");
      one('[data-view="trash"]').click(); await until(()=>btn(id("file-list"), "还原"), "trash list");
      assert(id("file-list").textContent.includes("Remover"), "deleted by name");
      const row = title => [...id("file-list").querySelectorAll("article")].find(r => r.querySelector("h3").textContent === title);
      assert(!one("#file-list h3 button, #file-list a"), "trash rows have no ambiguous detail links");
      assert(row("Archived first") && row("Archived second"), "two tombstones with same asset id");
      btn(row("Archived second"), "还原").click();
      await until(()=>!fileCenter.state.busy && !row("Archived second"), "restore selected image");
      assert(row("Archived first") && writes.at(-1).url === "/files/image/pic/restore" && writes.at(-1).body.trash_id === "second", "restore exact tombstone");
      btn(row("Guide"), "还原").click(); await until(()=>!fileCenter.state.busy && !row("Guide"), "restore");
      assert(writes.at(-1).body.trash_id === "trash1" && writes.at(-1).body.expected_generation === "g1", "restore contract");
    }
    document.body.dataset.result = "pass";
  } catch (e) { document.body.dataset.result = "FAIL: " + e.message; }
})();
