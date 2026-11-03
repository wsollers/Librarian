# Printed-page overrides

Detection handles most books. When it is wrong or empty (page numbers drawn as images,
unnumbered plates), add `pagemaps/<first 16 hex of sha256>.txt`. Rules apply in order:

```
# Enderton, A Mathematical Introduction to Logic (2nd)
1-13 none          # cover, front matter without numbers
14-29 roman 1      # PDF 14 is printed i
30-330 +13         # PDF 30 is printed 17
```

These files contain only numbers, so they belong in git. Rerun `librarian pagemap`.
