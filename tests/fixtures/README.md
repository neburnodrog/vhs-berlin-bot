# VHS Berlin fixtures

Raw HTML responses captured from the Berliner Volkshochschulen course
search, used as offline fixtures for parser tests. **Save and load as raw
bytes; decode with `windows-1252`.** (The HTTP `Content-Type` header on
responses says `iso-8859-15` but the page's own `<meta charset>` says
`windows-1252`; the latter is correct — umlauts decode cleanly.)

## Files

- `form-initial.html` — plain GET of the search form with no filters
  applied. The default tab is *Einfach* (simple).
- `search-district-31-page-1.html` — first page of results for district
  31 (Mitte) with no search term and default sort.
- `search-district-31-page-2.html` — page 2 of the same search, fetched
  via the DataGrid pager's next-page image button.

## Capture run

- Capture timestamp (UTC): 2026-05-29T17:29:09Z
- GET form-initial: url=https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx, status=200, content-type=text/html; charset=iso-8859-15, bytes=520753
- form-initial state: __VIEWSTATE len=211888, __VIEWSTATEGENERATOR=2B79C7F0, __EVENTVALIDATION len=absent
- district 31 (Mitte) checkbox index N = 5
- encoding: <meta>=windows-1252, Content-Type header says 'text/html; charset=iso-8859-15' (server lies — page actually decodes correctly as windows-1252)
- after Erweitert tab: __VIEWSTATE len=211924, __EVENTVALIDATION len=absent, district 31 idx=5
- POST search: status=200, final url=https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseList.aspx, bytes=104837 (server redirects from CourseSearch.aspx to CourseList.aspx)
- page1 state: __VIEWSTATE len=48620, __VIEWSTATEGENERATOR=03F8BC54, __EVENTVALIDATION len=1180
- page1 rows (CourseDetail.aspx?id refs): 10; Anzahl Treffer=312; pager label=Seite 1 von 32
- POST page2 (next button=ctl00$Content$ILDataGrid1$ctl01$ctl04): status=200, bytes=101092, url=https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseList.aspx
- page2 state: __VIEWSTATE len=45160, __EVENTVALIDATION len=1180
- page2 rows=10; pager label=Seite 2 von 32
- encoding sanity (decoded windows-1252 shows umlauts): True

## How to reproduce

Run a single `httpx.AsyncClient` (cookies persist) through this flow:

1. `GET https://www.vhsit.berlin.de/VHSKURSE/BusinessPages/CourseSearch.aspx`
2. Extract `__VIEWSTATE`, `__VIEWSTATEGENERATOR` from the response. (The
   form has **no** `__EVENTVALIDATION` field on the initial GET — it is
   either disabled server-side for this page, or only appended after
   certain interactions.)
3. `POST CourseSearch.aspx` with `ctl00$Content$lbtnTab2=Erweitert` and
   the hidden state fields. This switches the form to the *Erweitert*
   (advanced) tab where the Bezirk checkbox list lives.
4. Re-extract the state fields from the response, then
   `POST CourseSearch.aspx` with:
      - the refreshed state
      - `ctl00$Content$btnSearch=Suchen` (real submit-button value,
        because `btnSearch` is `type=submit` with `useSubmitBehavior=true`)
      - `ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$5=on`
        (index 5 is the row whose value is `31` = Mitte)
      - `ctl00$Content$AdvancedSearch1$SearchBox1$txtSearchTerm=` (empty)
   The server **302-redirects to `CourseList.aspx`**, which is the
   results page (its body is what `search-district-31-page-1.html`
   contains).
5. To get page 2: from the page-1 response, find the next-page image
   button — `<input type="image" name="ctl00$Content$ILDataGrid1$ctl01$ctl04" ... src="...arrow_right.svg">`.
   POST to `CourseList.aspx` with the refreshed state plus image-submit
   coordinates `name.x=5&name.y=5`.

## Key form-state field names

- Hidden: `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`
  (the last is **absent** on the initial GET; subsequent responses
  may or may not include it)
- Tab switches: `ctl00$Content$lbtnTab1` (Einfach), `lbtnTab2` (Erweitert),
  `lbtnTab3` (Kursnummer)
- Search submit: `ctl00$Content$btnSearch` (value `Suchen`)
- District checkbox list: `ctl00$Content$AreaListAdvanced1$CheckBoxListDistricts$N`,
  where N=5 corresponds to district id 31 (Mitte)
- Advanced search term: `ctl00$Content$AdvancedSearch1$SearchBox1$txtSearchTerm`
- DataGrid pager image inputs: `ctl00$Content$ILDataGrid1$ctl01$ctl01..ctl05`
  (leftend, left, [Seite N von M label], right, rightend)

## Result-row signal

Each course row on `CourseList.aspx` contains a link of the shape
`CourseDetail.aspx?id=<course-id>`. Rows are `<tr class="DataGridItem">`
and `<tr class="DataGridAlternatingItem">`. There are also `Booking` /
`Anmelden` hooks per row that the parser can use.

## Politeness

Captured with User-Agent:
`vhs-berlin-bot/0.1 (+https://github.com/rubenkarlsson/vhs-berlin-bot, contact: neburgordon@gmail.com)`
and a 2-second sleep between requests.
