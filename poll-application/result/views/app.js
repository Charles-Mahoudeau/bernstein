var app = angular.module('poll-app', []);
var socket = io.connect({transports:['polling']});

app.controller('statsCtrl', function($scope){
  $scope.aPercent = 25;
  $scope.bPercent = 25;
  $scope.cPercent = 25;
  $scope.dPercent = 25;

  $scope.topHeight = 50;
  $scope.bottomHeight = 50;
  $scope.aWidth = 50;
  $scope.bWidth = 50;
  $scope.cWidth = 50;
  $scope.dWidth = 50;

  var updateScores = function(){
    socket.on('scores', function (json) {
       data = JSON.parse(json);
       var a = parseInt(data.a || 0);
       var b = parseInt(data.b || 0);
       var c = parseInt(data.c || 0);
       var d = parseInt(data.d || 0);

       var percentages = getPercentages(a, b, c, d);
       var layout = getLayout(a, b, c, d);

       $scope.$apply(function () {
         $scope.aPercent = percentages.a;
         $scope.bPercent = percentages.b;
         $scope.cPercent = percentages.c;
         $scope.dPercent = percentages.d;
         $scope.total = a + b + c + d;

         $scope.topHeight = layout.topHeight;
         $scope.bottomHeight = layout.bottomHeight;
         $scope.aWidth = layout.aWidth;
         $scope.bWidth = layout.bWidth;
         $scope.cWidth = layout.cWidth;
         $scope.dWidth = layout.dWidth;
       });
    });
  };

  var init = function(){
    document.body.style.opacity=1;
    updateScores();
  };
  socket.on('message',function(data){
    init();
  });
});

function getPercentages(a, b, c, d) {
  var result = {};

  if (a + b + c + d > 0) {
    result.a = Math.round(a / (a + b + c + d) * 100);
    result.b = Math.round(b / (a + b + c + d) * 100);
    result.c = Math.round(c / (a + b + c + d) * 100);
    result.d = 100 - result.a - result.b - result.c;
  } else {
    result.a = result.b = result.c = result.d = 25;
  }

  return result;
}

function getLayout(a, b, c, d) {
  var result = {};
  var top = a + b;
  var bottom = c + d;
  var total = top + bottom;

  result.topHeight = total > 0 ? (top / total * 100) : 50;
  result.bottomHeight = 100 - result.topHeight;
  result.aWidth = top > 0 ? (a / top * 100) : 50;
  result.bWidth = 100 - result.aWidth;
  result.cWidth = bottom > 0 ? (c / bottom * 100) : 50;
  result.dWidth = 100 - result.cWidth;

  return result;
}
