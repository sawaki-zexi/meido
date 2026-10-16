using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public class Fixture : Form {
    public TextBox Input;
    public Button ActionButton;
    public TrackedPanel ScrollArea;
    public int Clicks, Keys, WheelV, WheelH;
    public string KeyEvents="";
    static string directory;
    static Fixture a, b;
    public Fixture(string title, int width, int height) {
        Text = title; Size = new Size(width, height); StartPosition = FormStartPosition.Manual;
        Location = new Point(40, 40); KeyPreview = true;
        var button = new Button { Text="Fixture button", AccessibleName="Fixture button", Location=new Point(20, 20), Size=new Size(150, 32) };
        ActionButton = button;
        button.Click += (s,e) => { Clicks++; Save(); while(File.Exists(Path.Combine(directory,"block-click"))) System.Threading.Thread.Sleep(10); };
        Input = new TextBox { AccessibleName="Fixture input", Location=new Point(20,65), Size=new Size(220,30) };
        Input.TextChanged += (s,e) => Save();
        Input.KeyDown += (s,e) => { Keys++; KeyEvents+=e.KeyCode.ToString()+","; Save(); };
        ScrollArea = new TrackedPanel { AccessibleName="Fixture scroll", Location=new Point(20,110), Size=new Size(240,140), AutoScroll=true, TabStop=true };
        ScrollArea.Controls.Add(new TrackedLabel { Text=new string('x',200), Location=new Point(0,0), Size=new Size(1400,1400) });
        ScrollArea.MouseWheel += (s,e) => { WheelV++; Save(); };
        Controls.AddRange(new Control[]{button, Input, ScrollArea});
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x20A || m.Msg == 0x20E) {
            if (ScrollArea != null) ScrollArea.Deliver(m.Msg, m.WParam);
        }
        base.WndProc(ref m);
    }
    object State() { return new { hwnd=Handle.ToInt64(), title=Text, text=Input.Text, clicks=Clicks, keys=Keys, key_events=KeyEvents,
        wheel_v=ScrollArea.Vertical, wheel_h=ScrollArea.Horizontal, directions=ScrollArea.Directions,
        scroll_x=ScrollArea.AutoScrollPosition.X, scroll_y=ScrollArea.AutoScrollPosition.Y,
        button_x=ActionButton.PointToScreen(new Point(ActionButton.Width/2,ActionButton.Height/2)).X,
        button_y=ActionButton.PointToScreen(new Point(ActionButton.Width/2,ActionButton.Height/2)).Y,
        enabled=Enabled, maximized=WindowState==FormWindowState.Maximized, x=Left, y=Top, width=Width, height=Height }; }
    static void Save() {
        if (a==null || b==null || !a.IsHandleCreated || !b.IsHandleCreated) return;
        string data = new JavaScriptSerializer().Serialize(new {pid=System.Diagnostics.Process.GetCurrentProcess().Id, a=a.State(), b=b.State()});
        try { File.WriteAllText(Path.Combine(directory,"state.next"),data); File.Copy(Path.Combine(directory,"state.next"),Path.Combine(directory,"state.json"),true); } catch(IOException) {}
    }
    [STAThread] public static void Main(string[] args) {
        directory=args[0]; Directory.CreateDirectory(directory);
        Application.EnableVisualStyles();
        a=new Fixture(args[1]+" A",430,340); b=new Fixture(args[1]+" B",300,280); b.Location=new Point(490,50);
        b.Show(); a.Show(); a.Activate();
        var timer=new Timer{Interval=50}; timer.Tick += (s,e)=> {
            if(File.Exists(Path.Combine(directory,"maximize-a"))) {a.WindowState=FormWindowState.Maximized;File.Delete(Path.Combine(directory,"maximize-a"));}
            Save(); if(File.Exists(Path.Combine(directory,"stop"))) {b.Close();a.Close();}
        }; timer.Start();
        Application.Run(a);
    }
}

public class TrackedPanel : Panel {
    public int Vertical, Horizontal;
    public string Directions="";
    public void Deliver(int message, IntPtr parameter) {
        int delta=(short)((parameter.ToInt64()>>16)&0xffff);
        if (message==0x20A) { Vertical++; Directions += delta>0?"up,":"down,"; }
        if (message==0x20E) { Horizontal++; Directions += delta>0?"right,":"left,"; }
    }
    protected override void WndProc(ref Message m) {
        if(m.Msg==0x20A || m.Msg==0x20E) Deliver(m.Msg,m.WParam);
        base.WndProc(ref m);
    }
}
public class TrackedLabel : Label {
    protected override void WndProc(ref Message m) {
        if ((m.Msg==0x20A || m.Msg==0x20E) && Parent is TrackedPanel) ((TrackedPanel)Parent).Deliver(m.Msg,m.WParam);
        base.WndProc(ref m);
    }
}
