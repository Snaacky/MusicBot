$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing, System.Xml, System -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.Text.RegularExpressions;
using System.Xml;

public static class ControlIconRasterizer {
    static float Number(string[] tokens, ref int index) {
        return float.Parse(tokens[index++], CultureInfo.InvariantCulture);
    }

    static GraphicsPath Parse(string data) {
        MatchCollection matches = Regex.Matches(data, @"[MLCZ]|-?\d+(?:\.\d+)?");
        string[] tokens = new string[matches.Count];
        for (int index = 0; index < matches.Count; index++) tokens[index] = matches[index].Value;
        GraphicsPath path = new GraphicsPath();
        PointF point = new PointF();
        int cursor = 0;
        while (cursor < tokens.Length) {
            string command = tokens[cursor++];
            if (command == "M") {
                path.StartFigure();
                point = new PointF(Number(tokens, ref cursor), Number(tokens, ref cursor));
            } else if (command == "L") {
                PointF end = new PointF(Number(tokens, ref cursor), Number(tokens, ref cursor));
                path.AddLine(point, end);
                point = end;
            } else if (command == "C") {
                PointF first = new PointF(Number(tokens, ref cursor), Number(tokens, ref cursor));
                PointF second = new PointF(Number(tokens, ref cursor), Number(tokens, ref cursor));
                PointF end = new PointF(Number(tokens, ref cursor), Number(tokens, ref cursor));
                path.AddBezier(point, first, second, end);
                point = end;
            } else if (command == "Z") {
                path.CloseFigure();
            } else {
                path.Dispose();
                throw new FormatException("Only original absolute M/L/C/Z paths are supported.");
            }
        }
        return path;
    }

    public static void Export(string source, string destination) {
        XmlDocument document = new XmlDocument();
        document.XmlResolver = null;
        document.Load(source);
        XmlElement svg = document.DocumentElement;
        float width = float.Parse(svg.GetAttribute("stroke-width"), CultureInfo.InvariantCulture);
        Color color = ColorTranslator.FromHtml(svg.GetAttribute("stroke"));
        using (Bitmap canvas = new Bitmap(512, 512, PixelFormat.Format32bppArgb)) {
            using (Graphics graphics = Graphics.FromImage(canvas)) {
                graphics.Clear(Color.Transparent);
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.ScaleTransform(512f / 24f, 512f / 24f);
                using (Pen pen = new Pen(color, width)) {
                    pen.StartCap = LineCap.Round;
                    pen.EndCap = LineCap.Round;
                    pen.LineJoin = LineJoin.Round;
                    foreach (XmlNode node in svg.ChildNodes) {
                        XmlElement element = node as XmlElement;
                        if (element == null || element.LocalName != "path") continue;
                        using (GraphicsPath path = Parse(element.GetAttribute("d"))) {
                            graphics.DrawPath(pen, path);
                        }
                    }
                }
            }
            using (Bitmap export = new Bitmap(128, 128, PixelFormat.Format32bppArgb)) {
                using (Graphics graphics = Graphics.FromImage(export)) {
                    graphics.Clear(Color.Transparent);
                    graphics.CompositingMode = CompositingMode.SourceCopy;
                    graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    graphics.DrawImage(canvas, new Rectangle(0, 0, 128, 128));
                }
                export.Save(destination, ImageFormat.Png);
            }
        }
    }
}
'@

foreach ($icon in Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.svg') {
    $destination = Join-Path $PSScriptRoot ($icon.BaseName + '.png')
    [ControlIconRasterizer]::Export($icon.FullName, $destination)
    Write-Output ($icon.BaseName + '.png')
}
