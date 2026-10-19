"""Bind visible HTML row quotes back to exact immutable source intervals."""
from html import unescape
from html.parser import HTMLParser
import re


class _VisibleRow(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=False)
        self.lines=[0]
        self.lines.extend(m.end() for m in re.finditer('\n',raw))
        self.chars=[];self.offsets=[]
        self.hidden=0

    def position(self):
        line,column=self.getpos()
        return self.lines[line-1]+column

    def append(self,value,start,end=None):
        if self.hidden:return
        for i,ch in enumerate(value):
            for folded in ch.casefold():
                if folded!='_' and re.fullmatch(r'\w',folded):
                    self.chars.append(folded)
                    self.offsets.append((start+i,start+i+1) if end is None else (start,end))

    def handle_data(self,data):self.append(data,self.position())
    def handle_entityref(self,name):
        raw='&'+name+';';start=self.position();self.append(unescape(raw),start,start+len(raw))
    def handle_charref(self,name):
        raw='&#'+name+';';start=self.position();self.append(unescape(raw),start,start+len(raw))
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)


def locate_html_row_quote(raw: str, collapsed_quote: str):
    """Match one visible row only; attributes and cross-row joins cannot cite.

    This proves a quote location, not that a generated proposition is true.
    Normal claim-support validation still runs after binding.
    """
    if not collapsed_quote:return None
    matches=[]
    for row in re.finditer(r'<tr\b[^>]*>.*?</tr\s*>',raw,re.I|re.S):
        parser=_VisibleRow(row.group())
        try:parser.feed(row.group());parser.close()
        except (ValueError,IndexError):continue
        visible=''.join(parser.chars);start=visible.find(collapsed_quote)
        while start>=0:
            end=start+len(collapsed_quote)-1
            matches.append((row.start()+parser.offsets[start][0],row.start()+parser.offsets[end][1]))
            start=visible.find(collapsed_quote,start+1)
    return matches[0] if len(matches)==1 else None
