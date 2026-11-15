# Phân tích Kỹ thuật Pipeline `PDF -> DOCX` và Xử lý Tiếng Việt của ABBYY FineReader 16 (Static Analysis via MCP `rea`)

- **Thư mục mục tiêu (Read-only):** `C:\Program Files\ABBYY FineReader 16` (1,571 files tại thời điểm khảo sát)
- **Công cụ phân tích:** MCP Server `rea` (`rea-agents@6.9.0`: `inspect_native_exports`, `inspect_pe_imports`, `inspect_pe_resources`)
- **Mục tiêu nghiệp vụ:** Tách bạch kiến trúc chuyển đổi `PDF -> DOCX` giữ bố cục (bảng biểu, cột, đoạn văn, heading, hình ảnh) và xử lý Tiếng Việt (`Vietnamese` / LCID `1066`).

---

## 1. Bảng Bằng chứng Phân tích Tĩnh (Observed Facts)

Toàn bộ các thông số dưới đây là **Observed Facts** trích xuất trực tiếp từ các PE32+ binary tại `C:\Program Files\ABBYY FineReader 16` thông qua MCP `rea`:

| Giai đoạn / Vai trò | Tên file | Kích thước (Bytes) | SHA-256 Digest | REA Evidence ID(s) | Bằng chứng Export / Import / Resource / RTTI quan sát được |
|---|---|---:|---|---|---|
| **CLI / UI Shell (PDF Editor)** | `FineReader.exe` | 7,880,504 | `dbd888498fc19638c54cbc8faacd963d6c6b12a9cf675ff52b23c8c88b1999f0` | `ev_7c63fe21b527162605534fa5510e15b69dec081ff8c2652c391148e7ef0dc64e` | Import `FineObj.dll` (485), `Awl.dll` (1045), `LangInfoUnicode.dll` (16). Chứa chuỗi `CommandLineInfo.cpp`, `PdfEngine::IPdfSupport`, `PdfEngine::IProcessPdfOptionsClass`, `FineReader\|Feature\|CommandLine`, `FineReader\|Feature\|UpgradeForCommandline`, `/scanPDFtoOCR "%0" "%1" "%2"`, `/noSplash`. |
| **OCR Editor & CLI Handler** | `finereaderocr.exe` | 4,267,832 | `d9ad2b5d932e9c3c6861d53ad7dcff317a62030f83c16d6661b5909f3ba62267` | `ev_04b1646686919531a989673388a01f672d2659d9ba6a68874cbaaa743a21d295` | Import `FineObj.dll` (415), `Awl.dll` (1135), `LangInfoUnicode.dll` (5 hàm: `GetAlphabet@Languages@LangInfo`, `GetNearestFromLCID@Languages@LangInfo`, ...). Chuỗi: `ABBYY.FineReader16.OCR.Application`, `FineReader.Integration16`, `/NoSplash`, `quit`, `.docx`, `MSWord`, `Vietnamese`. |
| **Hot Folder & Task Automation** | `HotFolder.exe`<br>`HFEngine.dll`<br>`taskautomation.dll` | 2,089,272<br>606,520<br>564,536 | `77e62cc47fa6e60db0fdc3e3f34eb9844606c18b4177f619ce2621b60cae4781`<br>`39c6df5c76a732beb9f8e01acf2fdaae0c9203185bec4ff839bbb2e20b74bf43`<br>`a17518bc721fecaa1e2341a50545991504f477084b90d462dc4fb700ad39c84c` | `ev_9690f1cf6ba8fe6efefb978cffb4196a1136f2e2c4051c95ac1e59de18fc18e5`<br>`ev_41e0095edff13c6d77dd5b6c6f6d2655dc636062384939b37b2d7023e0bc9f7e`<br>`ev_00ed6f55f133ec9e19bb53780891cc3fe869289971dff5390ec12fa97504f26b`<br>`ev_ea028c36aec852aadcb7d87c9ce2415704d5a7a085263135fbef81b69e0d32ad`<br>`ev_927831fc044c2035ac48184433e6a77cfc80b0a3a926619850ef0e0722655d17` | `HFEngine.dll` và `taskautomation.dll` chỉ export `__FineObjUsed` (1 symbol). Chứa RTTI `CExportOperation@TA`, `CLayoutBlocksOperation@TA`, `CExportOptionsModificator`, `OcrEngine.PdfExportProfileLoader`, `TaskAutomation.AnalyzeOperation`. |
| **Background OCR Host & Document** | `OcrEngine.Background.Host.exe`<br>`ocrengine.document.dll`<br>`documentprocessing.dll` | 44,856<br>4,348,216<br>1,409,336 | `4e84161b42fa7553ea8ca1b54da74d46b3a07acb54f9a1725d76dce9955b2977`<br>`a0608d7778afd8f8ed0faaf01c91adcb9bd031f4b1e36d66db84cc58533bf2fc`<br>`8a16a616ebba9552b35f283164a4818ca72b2bdfd6edddaab0b6170bec13fcf7` | `ev_c83654614a5f97b321d738826bc77feb3bcc7b678e31cb407824d395a9baef15`<br>`ev_750a71becbcecc9942e501ad78e39c34c4ed185f671a093e22cf1169942ed4c8`<br>`ev_66d76120c9d7d7f27311062bc7e4fc7af6017984040bba3e45138bdba52a45b3`<br>`ev_9dc301fbfd1a82f337e3fa29d6aeead0d8f13482ceae9a9f905683525e0dec42`<br>`ev_8e295ef7fa5b2b7c10beaed8ecc727fe26040cfba0f1518c55ee0ad2b21d9994` | `ocrengine.document.dll` & `documentprocessing.dll` chỉ export `__FineObjUsed`. Import `IsPDFFile@PdfTools`, `WorstQuality@CQuality@DocumentAnalysis`, `GetNearbyRectObjectsSet@CRectangularAreasMapBody@DocumentAnalysis`, `GetAlphabet@Languages@LangInfo`, `GetInterwordPunctuators@Languages@LangInfo`. |
| **Bước 1: PDF Engine & Text Heuristics** | `PdfEngine.dll`<br>`pdftools.heuristics.dll` | 2,899,256<br>133,944 | `f5fbc16de33b391f7b373cb4d45dc15c2cbb624994644c23adbf91f8f266e2b5`<br>`5387bfbd4e1146d8fd0bba991cec01aac6532fb82f23f5166b43b9e402e2c8cf` | `ev_b02a8c0e2d607d92918096843e183bdb821de8fbfab81e75d6c544059f5842ca`<br>`ev_515fecc42aa2a5c2381a7879893c06ee5903649c941c81f8481bc847d0c450f2`<br>`ev_b96ee1bd86a5584f936c565fd9d03ba9eb69d4d7796df25c14a2c50c3dcc15c9`<br>`ev_8fe3ef5d1bcb223409d228790593327e737a125ebd938a420ea0425102faa09b` | Cả 2 DLL chỉ export `__FineObjUsed`. `pdftools.heuristics.dll` import `CDiacriticSymbolCalculator@PdfTools` & `CLetterSymbolCalculator@PdfTools` từ `PdfTools.CommonInstruments.dll`; chứa RTTI `CBadEncodingDetector@PdfTools`, `CBadEncodingFontDetector@PdfTools`, `IExternalFontMapper@PdfTools`, `CPdfFontNameHelper@PdfTools`. |
| **Bước 2 & 3a: Layout, Objects & Table Capture** | `DocumentAnalysis.Objects.dll`<br>`TableCapture.dll`<br>`TextLayout.dll` | 262,456<br>501,048<br>1,442,616 | `c8fc105402b33aab28c2aa3ab6048bfa0367591831b16021f4ec78c0420decb8`<br>`053534960d8b7aefcf03073cfe6a0efaab3ef406bf26af92a649220853b8471e`<br>`c513dd7481f6bdb785cf3d4a4739d540486667e54b8afea120e0cf0ac00bb2e2` | `ev_307ab9299aaaecb7de792b7595d48b4b07ab941365501349173f6ddc81c77d56`<br>`ev_633350a40d0326c021a4b0681d265841db6e8f579c490ed03361dedb7828abd6`<br>`ev_d7690df7c47e7396c928020a1b272d1a4febcecfce0724b79fb7485717d1bad9`<br>`ev_a910962d86c17a721b1b7b67a5e69de8cf87ab961c7b0040faf3e8966729f313` | `DocumentAnalysis.Objects.dll` export 186 symbols (`CRectangularAreasMapBody`, `CRectangularAreaHorzIterator`, `CRectangularAreaVertIterator`, `CXVectorSimpleObject`). `TableCapture.dll` import `CTableNetFeaturesExtractor@DocumentAnalysis`, `CLstmLayer@NeoML`, `CDnnWrapper@NeoML`, `CYamlFileDnnSettingsReader@NeoML`. |
| **Bước 3b: Tổng hợp Bố cục ADRT** | `Synthesis.dll` | 2,768,696 | `8cefe275dc276d0844a381276f9512029b3a4df3cdce640a06a64218e249cb16` | `ev_ca9b33edb91eb7463ed6b8ce47ca3543951337df2a900e906f028b63fff262f5`<br>`ev_825ba5dfa5bc61fc0747d5f306c11e9c654b350a71035a64c6f2c7137d003132`<br>`ev_8e4693c71421ccab4dc5ab6d2daa8fc4a9805cd1a0f355d7e012c811e806c083` | Export `__FineObjUsed`. Import `NeoML.dll` (37), `LangInfoUnicode.dll` (31). Chứa RTTI/source paths: `PreOutLetter.cpp`, `PreOutWord.cpp`, `PreOutLine.cpp`, `PreOutParagraph.cpp`, `LayoutCorrector.cpp`, `ListMarkerCorrector.cpp`, `CHeadingClusterizer`, `CHeadingClusterizerByMainFeatures`, `COutPageColumn`, `COutPageFootNote`, `CFSSerializedPdfFontStylesStorage`. Resource có `ABBYY_SIGNATURE_1..5` và `DELAY_LOAD`. |
| **Bước 4: Unicode & Tiếng Việt** | `langinfounicode.dll`<br>`languagedatabase.dll`<br>`morphology.dll`<br>`Vietnamese.amd` | 2,334,008<br>194,360<br>1,278,776<br>282,928 | `21d9d099a87c2ea87b7386059cafd2705defbb90ccca934eb4bb6034bac4c359`<br>`87408c2ef1a65cafd7c3921a21f6d72a6b5d4b3ab2b5c5d23bd17834e7ea10f4`<br>`5aef847726034a17d70324a092586b8f7a7e49955320facd34d98be5140e6ee2`<br>— | `ev_c7c5f3b1f3b61a942a610dbbfd6b4f19238c06bc31d2befcda311f064df514cb`<br>`ev_cb2d19c711247678d254df8022378fd68cd158f07db3371affb08380ff60bf33`<br>`ev_12c076658fef43842928399e450a47ca80ae3c98232212b7184d54538e9bfe0e` | `langinfounicode.dll` export **225 symbols**, nổi bật: `?Compose@Unicode@LangInfo@@YA?AVCUnicodeString@FObj@@AEBV34@@Z`, `?ComposePrefix@Unicode@LangInfo`, `?Accents@LettersSets@LangInfo`, `?GetMainForm@Unicode@LangInfo`, `?GetShapedForm@Unicode@LangInfo`, `?GetAlphabet@Languages@LangInfo`, `?GetNearestFromLCID@Languages@LangInfo`. `Vietnamese.amd` chứa từ điển hình thái học Tiếng Việt. |
| **Bước 5: Xuất Word OpenXML (`.docx`)** | `export.wp.dll`<br>`Export.Common.dll` | 1,467,192<br>1,163,064 | `aeb704c487d0112f17b3d3344f76d961faa672b251dc8c78eb1f04b5be83333b`<br>`9f82bd61b2cfd38e9c967d973010daf63f3e4407aa556f5c01ab3c03ab519f22` | `ev_39eea186f6f9bb0c6aa6403276e242925a05fc6715036f723f9ae4906c8c8613`<br>`ev_06d423ebe4c06ba06d886211a686b637c7b2fc439036f836bd6bf0e1e7f538ea`<br>`ev_4ac0ecbeb3aeac321090561d04ed55bf79ecb091d99cee388ad5452d2f4d94fb`<br>`ev_a35a7f767773ccfbdeb4f6808c0dd8ef5721a0c5c2b0fd12b764f4af2857e4de` | `Export.Common.dll` export **827 symbols** (`COfficeOpenXMLPackage`, `CZipPackageXmlFile<TOfficeXmlScheme>`, `CCodePagesHistogram`). `export.wp.dll` chứa `CWordExporter@WordExporter`, `CEditableCopyIncutProcessor@WordExporter`, `CLikeStreamFramePropertiesBuilderHGPA`, `CLikeStreamFramePropertiesBuilderIPA`, `CRunningTitleSeries@WordExporter`, `CTableBox@WordDocument`, `CPictureBox@WordDocument`, `CTextBox@WordDocument`, và phát sinh các thẻ OpenXML `w:sectPr`, `w:cols`, `w:pPr`, `w:keepLines`, `w:rPr`, `w:tbl`, `w:tblPr`, `w:tblpPr`, `w:tblGrid`, `w:trPr`, `w:tcPr`, `w:tcBorders`, `v:textbox`. |

---

## 2. Kiến trúc Pipeline 5 Bước `PDF -> DOCX` (Observed Facts vs Inferences)

```text
[Input PDF]
   │
   ▼
[Bước 1: Phân loại PDF & Kiểm định Font/Encoding]
  (PdfEngine.dll + pdftools.heuristics.dll + pdfium.dll)
   ├── Text layer hợp lệ ──────────────────────────┐
   └── Scan / Thiếu ToUnicode / Lỗi dấu (CBadEncodingDetector)
          │                                        │
          ▼                                        │
[Bước 2: Rasterize & OCR Tiếng Việt]               │
  (PdfTools.Rasterizator.Pdfium.dll + RecPage.dll  │
   + Icr.CnnGraphemeRecognizer.dll + Vietnamese.amd)
          │                                        │
          └────────────────────┬───────────────────┘
                               ▼
[Bước 3: Document Analysis, Table Capture & ADRT Synthesis]
  (DocumentAnalysis.Segmentation.dll + TableCapture.dll + Synthesis.dll)
   - Bắt bảng lai CNN/LSTM (CTableNetFeaturesExtractor + CLstmLayer@NeoML)
   - Gom cụm ký tự -> từ -> dòng -> đoạn (PreOutLetter..PreOutParagraph)
   - Phân cụm Heading (CHeadingClusterizerByMainFeatures), Cột (COutPageColumn), Chân trang (COutPageFootNote)
                               │
                               ▼
[Bước 4: Chuẩn hóa Unicode NFC & Xử lý Tiếng Việt]
  (langinfounicode.dll + morphology.dll + Vietnamese.amd)
   - Compose@Unicode@LangInfo (NFD -> Unicode NFC dựng sẵn)
   - Kiểm tra dấu thanh/ký tự đặc thù (CDiacriticSymbolCalculator, LettersSets::Accents)
   - Chuẩn hóa khoảng trắng dấu câu (GetInterwordPunctuators)
                               │
                               ▼
[Bước 5: Đóng gói OpenXML Word (.docx)]
  (Export.Common.dll + export.wp.dll)
   - COfficeOpenXMLPackage -> word/document.xml, styles.xml, header/footer
   - Chế độ EditableCopy (CEditableCopyIncutProcessor, luồng văn bản tự nhiên) vs ExactCopy (CTextBox / w:tblpPr)
```

### Chi tiết cơ chế xử lý Cấu trúc & Tiếng Việt (Inferences dựa trên Observed Symbols)

1. **Phân biệt Digital PDF vs Scanned/Corrupted-Font PDF (Bước 1):**
   - *Observed:* `pdftools.heuristics.dll` tích hợp `CBadEncodingDetector@PdfTools`, `CBadEncodingFontDetector@PdfTools` và nhập `CDiacriticSymbolCalculator@PdfTools`, `CLetterSymbolCalculator@PdfTools`.
   - *Inference:* Nhiều file PDF Tiếng Việt cũ (dùng font VNI-Times, .VnTime/TCVN3 hoặc PDF xuất từ máy in ảo thiếu bảng `ToUnicode` CMap) vẫn có text layer nhưng khi copy ra sẽ bị mất dấu, biến thành ký tự rác hoặc sai dấu thanh. ABBYY dùng bộ đếm dấu (`CDiacriticSymbolCalculator`) và bộ phát hiện font lỗi mã (`CBadEncodingFontDetector`) để tự động chuyển những trang đó sang nhánh rasterize (`PdfTools.Rasterizator.Pdfium.dll`) và OCR lại thay vì tin tưởng mù quáng vào text layer.

2. **Nhận diện Bảng biểu & Cấu trúc Tài liệu ADRT (Bước 3):**
   - *Observed:* `TableCapture.dll` sử dụng kiến trúc hybrid giữa hình học (`CRectangularAreasMapBody`, `CRectangularAreaHorzIterator`, `CRectangularAreaVertIterator`) và mạng nơ-ron (`CTableNetFeaturesExtractor@DocumentAnalysis`, `CLstmLayer@NeoML`). `Synthesis.dll` có chuỗi lớp từ thấp lên cao: `PreOutLetter` → `PreOutWord` → `PreOutLine` → `PreOutParagraph`, đi kèm `CHeadingClusterizerByMainFeatures`, `ListMarkerCorrector`, `COutPageColumn`, `COutPageFootNote`.
   - *Inference:* Thay vì đặt mọi dòng vào hộp văn bản (TextBox) cố định, bộ ADRT (`Synthesis.dll`) phân cụm cỡ chữ/kiểu chữ trên toàn tài liệu (`CFSSerializedPdfFontStylesStorage`) để nhận ra Heading, tái tạo ranh giới cột (`COutPageColumn`), loại bỏ đầu/chân trang lặp lại (`CRunningTitleSeries`), và nối các dòng bị ngắt cứng trong cùng một đoạn văn.

3. **Chuẩn hóa Tiếng Việt Unicode NFC (Bước 4):**
   - *Observed:* `langinfounicode.dll` export hàm `LangInfo::Unicode::Compose` (`?Compose@Unicode@LangInfo@@YA?AVCUnicodeString@FObj@@AEBV34@@Z`), `ComposePrefix`, `LettersSets::Accents`, cùng bộ đối chiếu mã ngôn ngữ `Languages::GetNearestFromLCID` (Windows LCID của Tiếng Việt là `1066`).
   - *Inference:* Khi trích xuất từ PDF (đặc biệt PDF tạo trên macOS hoặc các trình tạo PDF chia tách dấu thanh tổ hợp NFD), các ký tự như `ă, â, đ, ê, ô, ơ, ư` kết hợp với 5 dấu thanh (`\u0300`, `\u0301`, `\u0303`, `\u0309`, `\u0323`) dễ bị tách thành 2-3 codepoint gây rớt dấu hoặc sai font fallback trong Word. Hàm `Compose` chuẩn hóa chuỗi về **Unicode dựng sẵn (NFC)** trước khi đối chiếu từ điển `Vietnamese.amd` và ghi ra XML.

4. **Chế độ Xuất OpenXML `.docx` (`export.wp.dll` & `Export.Common.dll`) (Bước 5):**
   - *Observed:* `export.wp.dll` chứa các lớp `CWordExporter`, `CEditableCopyIncutProcessor`, `CLikeStreamFramePropertiesBuilderHGPA` / `IPA`, `CTableBox`, `CPictureBox`, `CTextBox` và các phần tử OpenXML `w:sectPr`, `w:cols`, `w:pPr`, `w:keepLines`, `w:tbl`, `w:tblPr`, `w:tblpPr`, `w:tblGrid`, `w:trPr`, `w:tcPr`, `w:tcBorders`, `v:textbox`.
   - *Inference:*
     - **`EditableCopy` (Khuyên dùng cho chỉnh sửa):** Dùng `CEditableCopyIncutProcessor` và `CLikeStreamFramePropertiesBuilder*` để chuyển nội dung thành dòng chảy văn bản (`w:p`), bảng biểu thật (`w:tbl`), chia cột (`w:cols`), hạn chế tối đa `v:textbox` rời rạc.
     - **`ExactCopy`:** Sử dụng `CTextBox` (`v:textbox`) và `w:tblpPr` (floating table positioning) để khóa tọa độ từng khối giống hệt trang PDF gốc.
     - **`FormattedText`:** Giữ định dạng đoạn, font (`w:rPr`), bảng (`w:tbl`), bỏ các cấu trúc khung nổi/chia cột phức tạp.

---

## 3. Đánh giá Hướng A (CLI / COM / DLL Adapter) vs Hướng B (Clean-Room Pipeline)

1. **Gọi trực tiếp DLL nội bộ (C-ABI / `ctypes`):** **Không khả thi trực tiếp.**
   - *Bằng chứng (`inspect_native_exports`):* Toàn bộ các DLL lõi (`export.wp.dll`, `Synthesis.dll`, `TableCapture.dll`, `PdfEngine.dll`, `pdftools.heuristics.dll`, `ocrengine.document.dll`, `documentprocessing.dll`) đều chỉ export duy nhất 1 hàm là `__FineObjUsed` (ordinal 1).
   - Các đối tượng nội bộ được đăng ký và khởi tạo qua cơ chế factory khép kín của `FineObj.dll` (`?RegisterCreateObjectFunction@FObj`, `?DoCreateObject@FObj`) và kiểm tra chữ ký resource nội bộ (`ABBYY_SIGNATURE_1`, `ABBYY_SIGNATURE_2`, `ABBYY_SIGNATURE_4`, `ABBYY_SIGNATURE_5`).
2. **Gọi qua CLI (`FineReader.exe` / `finereaderocr.exe`):** **Hạn chế đối với chế độ chạy ngầm hoàn toàn.**
   - *Bằng chứng:* Trong bản cài đặt này không có `FineCmd.exe` (chỉ có ở gói FineReader Server / FineReader Engine SDK hoặc cấu hình CLI riêng). Các binary `FineReader.exe` và `finereaderocr.exe` kiểm tra feature flag `FineReader|Feature|CommandLine` (`UpgradeForCommandline`) và gắn với cửa sổ giao diện chính (`FineReader16MainWindowClass`, `Awl.dll` với >1,000 UI imports).
   - Tuy nhiên, có thể đóng gói thêm một adapter tùy chọn (`AbbyyLocalAdapter`) gọi `finereaderocr.exe` / `FineReader.exe` khi người dùng chỉ định rõ `--engine abbyy`.
3. **Hướng B — Clean-Room Pipeline (Đề xuất chính):**
   - Tái tạo trọn vẹn kiến trúc 5 bước đã bóc tách từ ABBYY bằng Python thuần (`PyMuPDF` + `pdf2docx` + `python-docx` + `RapidOCR` + bộ chuẩn hóa `Unicode NFC` & kiểm định bảng mã Tiếng Việt), hoàn toàn không phụ thuộc UI, không đụng chạm bản quyền hay binary của ABBYY, chạy nhanh bằng 1 câu lệnh duy nhất.
